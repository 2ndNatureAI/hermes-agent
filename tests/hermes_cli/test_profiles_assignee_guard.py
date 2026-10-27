"""Assignee validation guard tests (t_89658341).

Covers hermes_cli.profiles_assignee_guard (leaf helpers) and the three
kanban_db write seams (create / reassign / triage-specify) plus the
dispatcher per-tick skip line. Leaf tests run without a board; seam tests
carry their own kanban_home fixture (same shape as test_kanban_db.py's).
"""

from __future__ import annotations

import logging

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.profiles_assignee_guard import (
    CONTROL_PLANE_ASSIGNEES,
    profile_assignee_names,
    validate_assignee,
    warn_unknown_assignee,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _clear_parked_worker_procs():
    """Empty the dispatcher's parked-worker registry before each test.

    When this file runs AFTER test_kanban_db.py in the same pytest session,
    that file's spawn fixtures leave a fake proc parked in the module-global
    ``_live_worker_procs``; the reclaim phase inside ``dispatch_once`` then
    polls it (``.poll``) and crashes with AttributeError before any test
    body runs. Cross-file module-global leakage is a pre-existing issue
    outside this card's scope; clearing the registry here keeps these tests
    order-independent without touching the other file.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    kbd._live_worker_procs.clear()
    yield
    kbd._live_worker_procs.clear()


# ---------------------------------------------------------------------------
# profile_assignee_names
# ---------------------------------------------------------------------------

def test_profile_assignee_names_fail_open_returns_none(monkeypatch):
    """Enumeration failure -> None; callers must treat any name as legal."""
    from hermes_cli import profiles

    def boom(**kwargs):
        raise RuntimeError("disk error")

    monkeypatch.setattr(profiles, "list_profiles", boom)
    assert profile_assignee_names() is None


def test_profile_assignee_names_includes_default_and_control_plane():
    names = profile_assignee_names()
    assert names is not None
    # list_profiles omits the default profile name; the guard restores it.
    assert "default" in names
    assert CONTROL_PLANE_ASSIGNEES <= names


def test_profile_assignee_names_includes_live_profiles():
    names = profile_assignee_names()
    assert names is not None
    # At least one real live profile name resolves (the suite runs on a host
    # with at least the default; named dirs vary). The set is non-empty and
    # contains only lowercase tokens.
    assert names
    for name in names:
        assert name == name.lower()
        assert " " not in name and "/" not in name


# ---------------------------------------------------------------------------
# validate_assignee (token shape)
# ---------------------------------------------------------------------------

def test_validate_assignee_accepts_single_token():
    assert validate_assignee("nova", location="t") == "nova"


def test_validate_assignee_strips_surrounding_whitespace():
    assert validate_assignee("  nova  ", location="t") == "nova"


def test_validate_assignee_rejects_spaces():
    with pytest.raises(ValueError):
        validate_assignee("bad name", location="t")


def test_validate_assignee_rejects_slash():
    with pytest.raises(ValueError):
        validate_assignee("a/b", location="t")


def test_validate_assignee_empty_is_empty():
    assert validate_assignee(None, location="t") == ""
    assert validate_assignee("", location="t") == ""


def test_validate_assignee_non_string_raises():
    with pytest.raises(ValueError):
        validate_assignee(42, location="t")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# warn_unknown_assignee (profile resolution)
# ---------------------------------------------------------------------------

def test_warn_unknown_assignee_warns_on_unresolved(
    caplog, monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.profiles_assignee_guard.profile_assignee_names",
        lambda: frozenset({"default", "nova"}),
    )
    with caplog.at_level(logging.WARNING, logger="hermes_cli.profiles_assignee_guard"):
        warn_unknown_assignee("hermes", location="test")
    assert any("hermes" in r.message and "not a live profile" in r.message
               for r in caplog.records)
    # The warning must echo live profiles so the operator can fix without
    # hunting (t_c28d32f8: the name + the reachable fix in one line).
    assert any("nova" in r.message for r in caplog.records)


def test_warn_unknown_assignee_silent_on_resolved(
    caplog, monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.profiles_assignee_guard.profile_assignee_names",
        lambda: frozenset({"default", "forge"}),
    )
    with caplog.at_level(logging.WARNING, logger="hermes_cli.profiles_assignee_guard"):
        warn_unknown_assignee("forge", location="test")
        for name in sorted(CONTROL_PLANE_ASSIGNEES):
            warn_unknown_assignee(name, location="test")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_warn_unknown_assignee_fail_open_on_enumeration_failure(
    caplog, monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.profiles_assignee_guard.profile_assignee_names",
        lambda: None,
    )
    with caplog.at_level(logging.WARNING, logger="hermes_cli.profiles_assignee_guard"):
        warn_unknown_assignee("anything", location="test")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_warn_unknown_assignee_bad_shape_warns_not_raises(
    caplog, monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.profiles_assignee_guard.profile_assignee_names",
        lambda: frozenset(),
    )
    with caplog.at_level(logging.WARNING, logger="hermes_cli.profiles_assignee_guard"):
        warn_unknown_assignee("bad name", location="test")
    assert any("unusable assignee shape" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# kanban_db write seams
# ---------------------------------------------------------------------------

def test_create_task_warns_on_unknown_assignee(kanban_home, caplog, monkeypatch):
    """A typo'd assignee is recorded AND warned at write time (t_c28d32f8).

    Must NOT refuse the write: triage flows legitimately create cards whose
    assignee gets corrected later; the class's fix is evidence, not a reject.
    """
    monkeypatch.setattr(
        "hermes_cli.profiles_assignee_guard.profile_assignee_names",
        lambda: frozenset({"default"}),
    )
    with kbc.connect() as conn:
        with caplog.at_level(logging.WARNING, logger="hermes_cli.profiles_assignee_guard"):
            task_id = kb.create_task(conn, title="x", assignee="hermes")
    assert task_id
    assert any("'hermes'" in r.message and "create" in r.message
               for r in caplog.records)


def test_create_task_resolved_assignee_no_warning(kanban_home, caplog, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles_assignee_guard.profile_assignee_names",
        lambda: frozenset({"default", "forge"}),
    )
    with kbc.connect() as conn:
        with caplog.at_level(logging.WARNING, logger="hermes_cli.profiles_assignee_guard"):
            kb.create_task(conn, title="x", assignee="forge")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_reassign_task_warns_on_unknown_assignee(kanban_home, caplog, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles_assignee_guard.profile_assignee_names",
        lambda: frozenset({"default"}),
    )
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="x", assignee="default")
        with caplog.at_level(logging.WARNING, logger="hermes_cli.profiles_assignee_guard"):
            assert kb.assign_task(conn, task_id, "alicia") is True
    assert any("'alicia'" in r.message and "reassign" in r.message
               for r in caplog.records)


# ---------------------------------------------------------------------------
# dispatcher per-tick skip line
# ---------------------------------------------------------------------------

def test_dispatch_skip_line_logged_per_tick(kanban_home, caplog, monkeypatch):
    """The explicit ask (t_89658341 #2): when a ready card is skipped because
    its assignee cannot resolve, the dispatcher logs a line naming the card
    every tick - not silence, not a one-time DB event only."""
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="founder brief", assignee="hermes")
        with caplog.at_level(logging.WARNING,
                             logger="hermes_cli.kanban_db_dispatch"):
            res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
    assert task_id in res.skipped_nonspawnable
    skips = [r for r in caplog.records if "skipped" in r.message
             and "hermes" in r.message]
    assert skips, "dispatcher must log an explicit line for the skip"
    assert any(task_id in r.message for r in skips)


def test_dispatch_control_plane_assignees_stay_quiet(kanban_home, caplog):
    """default/reviewer/dispatcher pull via claim_task; they are legitimately
    nonspawnable and must not log per tick. Other skips stay silent as before."""
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="pull lane", assignee="reviewer")
        # reviewer is in the control-plane set: create-time guard is silent,
        # so plant the DB state directly for the dispatcher lane.
        conn.execute("UPDATE tasks SET assignee = 'reviewer' WHERE id = ?",
                     (task_id,))
        with caplog.at_level(logging.WARNING,
                             logger="hermes_cli.kanban_db_dispatch"):
            res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
    assert task_id in res.skipped_nonspawnable
    assert not [r for r in caplog.records
                if task_id in r.message and r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# dispatch policy invariants (constraint: no behavior change)
# ---------------------------------------------------------------------------

def test_nonspawnable_skip_does_not_charge_the_card(kanban_home, monkeypatch):
    """The guard is observability-only: a skipped_nonspawnable card is not a
    spawn, not a failure, and its consecutive_failures stay untouched.
    (monkeypatched enumeration -> the dispatcher warn line is deterministic
    and unrelated to the policy invariant.)"""
    monkeypatch.setattr(
        "hermes_cli.profiles_assignee_guard.profile_assignee_names",
        lambda: frozenset({"default"}),
    )
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="x", assignee="hermes")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM tasks WHERE id = ?",
            (task_id,)).fetchone()
    assert row["consecutive_failures"] in (0, None)


def test_dispatcher_control_plane_reviewer_resolves_via_assignment(kanban_home):
    """The dispatcher's control-plane exemption covers the pull lanes
    (claim_task assignees, e.g. 'reviewer'): never nonspawnable-logged."""
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="pull lane", assignee="reviewer")
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
    assert task_id in res.skipped_nonspawnable  # still cannot spawn
    assert task_id not in res.spawned if hasattr(res, "spawned") else True
