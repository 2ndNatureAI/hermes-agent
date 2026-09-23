"""Regression for audit finding F7 (cross-audit lane 3, card t_8a63f568).

A dispatcher worker spawned three cards with ``workspace_kind='local'`` — a
kind the dispatcher never supported — and each card burned a worker spawn
before dying with ``workspace: unknown workspace_kind: local``. The tool
surface must refuse an unknown ``workspace_kind`` at creation time with an
actionable error naming the valid values, so a bad kind can never reach the
dispatch path.

Live probe on 2026-09-22 showed kanban_create already refuses ``local``
(the ``VALID_WORKSPACE_KINDS`` check in ``create_task`` predates the incident
cards) — but nothing in the test suite locked that contract, and the incident
cards were actually injected by a raw SQL INSERT (the debrief cron's
ISSUE-TO-KANBAN contract) that bypasses ``create_task`` entirely. These tests
lock the tool surface; the raw-SQL vector is repaired operationally.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def creator_env(monkeypatch, tmp_path):
    """A dispatcher-owned worker (kanban_create's normal invoker): isolated
    HERMES_HOME, no delegated-child marker, no ambient task ownership."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    conn.close()
    return home


def test_create_rejects_unknown_workspace_kind(creator_env):
    """kanban_create with workspace_kind='local' must fail at creation with an
    actionable error naming the valid values — never reach dispatch."""
    from agent import delegation_context as dc
    from tools import kanban_tools as kt

    token = dc.enter_non_dispatcher_owned_context()
    try:
        out = kt._handle_create({
            "title": "probe local kind",
            "assignee": "peer",
            "workspace_kind": "local",
        })
    finally:
        dc.exit_non_dispatcher_owned_context(token)

    d = json.loads(out)
    assert "error" in d, f"expected rejection, got: {out}"
    assert "workspace_kind" in d["error"]
    for kind in ("scratch", "dir", "worktree"):
        assert kind in d["error"], f"error must name valid kinds, got: {d['error']}"

    # Nothing was written: the rejected kind must not appear on any card.
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE workspace_kind = 'local'"
        ).fetchone()
        assert row[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["scratch", "dir", "worktree"])
def test_create_accepts_documented_kinds(creator_env, kind):
    """The three documented kinds must still be accepted at creation."""
    from agent import delegation_context as dc
    from tools import kanban_tools as kt

    token = dc.enter_non_dispatcher_owned_context()
    try:
        args = {"title": f"probe {kind}", "assignee": "peer", "workspace_kind": kind}
        if kind == "dir":
            args["workspace_path"] = str(creator_env / "ws")
        out = kt._handle_create(args)
    finally:
        dc.exit_non_dispatcher_owned_context(token)

    d = json.loads(out)
    assert d.get("ok") is True, f"valid kind {kind!r} rejected: {out}"
    assert d["workspace_kind"] == kind
