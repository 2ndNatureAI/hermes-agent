# ---------------------------------------------------------------------------
# t_27cf3f73 repairs: integrated dispatch-seam contract (reviewer confirmation)
# ---------------------------------------------------------------------------
# These tests run the REAL _dispatch_lane_task seam inside a REAL isolated
# kanban DB (kanban_home), not a hand-built fixture, and assert the three
# reviewer D2/D3 findings are fixed at the seam:
#   1. fallback pick through the real seam carries full wall attribution
#      (walled_model/walled_provider from the real attribution resolution, not
#      helper-supplied tuples);
#   2. claim-time cleanup re-syncs the claimed Task object with the DB row;
#   3. eligibility pins hold end to end with the live allowlist config.
#
# kanban_home fixture (isolated HERMES_HOME + real schema via init_db, the
# canonical dispatcher-test harness) then drives patches:
#   - quota_fallback.fallbacks + providers.merge.models config declared
#     (so _lane_eligible/D2 resolve through the REAL config reader);
#   - respawn-guard cooldown set to 0 (no protocol interference);
#   - HERMES_KANBAN_QUOTA_FALLBACK stays unset — the gate is config-only;
#     the default lane list is NOT used, the config list is the whole list.
# The seam persists the override via _persist_fallback_override (real write
# path) inside the real dispatch txn context.
# ---------------------------------------------------------------------------

from pathlib import Path
from unittest.mock import MagicMock, patch
import json
import time

import pytest

import hermes_cli.kanban_quota_fallback as qf
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a REAL (empty) kanban DB schema — the
    canonical harness pattern for dispatcher tests (init_db, not hand-fabricated
    CREATE TABLE blocks), so _dispatch_lane_task can claim through the real
    claim_tasks path against a real tasks row (AUTOINCREMENT ids, task_events,
    task_runs, task_events schema)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


@pytest.fixture
def quota_env(tmp_path, monkeypatch):
    """A REAL (writable) config.yaml carrying the quota_fallback router and
    the merge allowlist — so D1/D2 resolution runs through the real config
    reader, not a stub, and D4 telemetry writes live to this tmp home."""
    ledger_dir = tmp_path / "provider-walls"
    ledger_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_KANBAN_QUOTA_FALLBACK", "")  # env unset: config gate only
    _cfg_dir = tmp_path / ".hermes"
    _cfg_dir.mkdir(exist_ok=True)
    cfg_path = _cfg_dir / "config.yaml"
    cfg_path.write_text(
        "providers:\n"
        "  merge:\n"
        "    models:\n"
        "      - zai/glm-5.3-flash\n"
        "      - deepseek/deepseek-v4-flash-0731\n"
        "  routers:\n"
        "    quota_fallback:\n"
        "      enabled: true\n"
        "      fallbacks:\n"
        "        - model: zai/glm-5.3-flash\n"
        "          provider: merge\n",
        encoding="utf-8",
    )
    # The REAL config reader reads the tmp YAML directly — D1/D2 resolve
    # through hermes_cli.config, not a stub (fixture-isolated).
        # The REAL config reader must resolve under the tmp HERMES_HOME
    # (kanban_home already repointed it). Assert, don't just assume.
    import hermes_cli.config as _hconfig
    resolved = _hconfig.read_raw_config_readonly() or {}
    expected_providers = {'merge': {'models': ['zai/glm-5.3-flash', 'deepseek/deepseek-v4-flash-0731']}, 'routers': {'quota_fallback': {'enabled': True, 'fallbacks': [{'model': 'zai/glm-5.3-flash', 'provider': 'merge'}]}}}
    if resolved.get("providers") != expected_providers:
        pytest.fail(
            f"quota_env: real reader did not resolve tmp config (providers="
            f"{resolved.get('providers')!r})")
    # D4 telemetry isolation — never touch the live %LOCALAPPDATA% ledger.
    monkeypatch.setattr(qf, "_ledger_dir", lambda: ledger_dir)
    return ledger_dir


def _seed_walled_run(conn, tid, model="gpt-6-luna", provider="openai-codex",
                     ended_offset=600.0):
    """Insert the latest ended run as rate_limited with a full claim stamp so
    the wall detection path resolves under D1.3a (the 47/60 stamped case)."""
    ended = int(time.time()) - int(ended_offset)
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata)"
        " VALUES (?, 'default', 'rate_limited', ?, ?, 'rate_limited', ?)",
        (tid, int(time.time()) - int(ended_offset) - 60, ended,
         json.dumps({"model": {"model": model, "provider": provider, "effort": "ultra"}})),
    )
    conn.commit()


def _lane_ready(conn, tid, assignee="default"):
    """Set the task ready (as after a rate-limit requeue)."""
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?", (tid,))
    conn.commit()


def _latest_fallback_event(conn, tid):
    # The dispatcher also writes a normal 'spawned' event in the same
    # dispatch — newest overall is not necessarily the fallback pick.
    row = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "AND kind = 'quota_fallback_lane_picked' "
        "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    if row is None:
        return None, {}
    return row["kind"], json.loads(row["payload"] or "{}")


# ---------------------------------------------------------------------------
# 1. attribution through the real seam (the reviewer's probe 2 shape)
# ---------------------------------------------------------------------------

def test_real_seam_persisted_event_carries_full_attribution(
    conn, quota_env, monkeypatch, all_assignees_spawnable,
):
    """The persisted override event written BY THE REAL SEAM — not a helper
    called with hand-supplied tuples — carries the resolved walled attribution.
    This is the integrated counterexample QUOTA_FALLBACK_REVIEW_PROBES.json
    found (dispatch:2257 called _persist_fallback_override with the walled
    tuple truncTed) — proving it's gone at the real seam."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    tid = kb.create_task(conn, title="walled-needs-attr", assignee="default")
    _seed_walled_run(conn, tid)
    _lane_ready(conn, tid)

    spawned = []
    ok = kbd._dispatch_lane_task(
        conn,
        conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone(),
        "default", kbd.DispatchResult(), lane="ready", dry_run=False,
        ttl_seconds=600, board=None, failure_limit=2,
        spawn_fn=lambda t, w, board=None: (spawned.append(t.id) or 424242),
        per_profile_cap=None, per_profile_running={},
    )
    assert ok and spawned == [tid]
    row = conn.execute(
        "SELECT model_override, provider_override FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["model_override"] == "zai/glm-5.3-flash"
    assert row["provider_override"] == "merge"
    kind, payload = _latest_fallback_event(conn, tid)
    assert kind == "quota_fallback_lane_picked"
    assert payload["model"] == "zai/glm-5.3-flash"
    assert payload["provider"] == "merge"
    # The attribution the reviewer found dropped at dispatch:2257:
    assert payload["walled_model"] == "gpt-6-luna"
    assert payload["walled_provider"] == "openai-codex"
    assert payload["source"] == "dispatch.quota_fallback"
    assert payload.get("persisted") is not False  # real persist happened


def test_real_seam_degraded_event_carries_full_attribution(
    conn, quota_env, monkeypatch, all_assignees_spawnable,
):
    """When persistence fails (write-path error), the degraded audit event the
    real seam writes must STILL carry the resolved walled attribution — the
    in-memory fallback audit never drops the wall evidence either."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    tid = kb.create_task(conn, title="walled-persist-fails", assignee="default")
    _seed_walled_run(conn, tid)
    _lane_ready(conn, tid)

    with patch(
        "hermes_cli.kanban_quota_fallback._persist_fallback_override",
        return_value=False, raising=False,
    ):
        ok = kbd._dispatch_lane_task(
            conn,
            conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone(),
            "default", kbd.DispatchResult(), lane="ready", dry_run=False,
            ttl_seconds=600, board=None, failure_limit=2,
            spawn_fn=lambda t, w, board=None: 424242,
            per_profile_cap=None, per_profile_running={},
        )
    assert ok
    kind, payload = _latest_fallback_event(conn, tid)
    assert kind == "quota_fallback_lane_picked"
    assert payload.get("persisted") is False
    assert payload["walled_model"] == "gpt-6-luna"
    assert payload["walled_provider"] == "openai-codex"


# ---------------------------------------------------------------------------
# 2. claim-time cleanup re-syncs the claimed Task object (the reviewer probe 3)
# ---------------------------------------------------------------------------

def test_claim_lane_re_syncs_task_after_cleanup(conn, quota_env, monkeypatch):
    """Offline probe shape (DB None / task object stale merge/GLM): after a
    fallback-authored override is cleaned under the D3 contract, the SAME
    claimed Task object the dispatch seam continues to use reflects the
    cleared state — claim-time stamping can no longer disagree with the board."""
    now = time.time()
    tid = kb.create_task(conn, title="redo-claims-fallback", assignee="default")
    # Aged-out wall history: latest ended run completed, no rate_limited runs inside the window.
    ended = int(now) - qf.WALL_RECENT_WINDOW_SECONDS - 60
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata)"
        " VALUES (?, 'default', 'completed', ?, ?, 'completed', '{}')",
        (tid, ended - 60, ended))
    conn.commit()
    # A fallback-authored override + its pick event on the row.
    conn.execute(
        "UPDATE tasks SET model_override='zai/glm-5.3-flash', provider_override='merge' WHERE id = ?",
        (tid,))
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, NULL, 'quota_fallback_lane_picked', ?, ?)",
        (tid, json.dumps({"model": "zai/glm-5.3-flash", "provider": "merge",
                           "source": "dispatch.quota_fallback", "run_id": None}), int(now)))
    conn.commit()

    task = kb.get_task(conn, tid)
    assert task.model_override == "zai/glm-5.3-flash"  # stale view before the seam
    res = qf.claim_lane(conn, task, inject_now=now)
    assert res is None  # no wall, nothing to route
    # DB row cleared...
    row = conn.execute(
        "SELECT model_override, provider_override FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row["model_override"], row["provider_override"]) == (None, None)
    # ...and the claimed OBJECT synchronized (the reviewer's probe found it stayed stale).
    assert task.model_override is None
    assert task.provider_override is None


def test_manual_override_survives_cleanup_and_stays_on_claim_object(
    conn, quota_env,
):
    """Newest-rule protection (preserved per reviewer): a manual
    model_override_set is never cleaned, and its claimed Task object stays
    coherent with the board in the other direction too."""
    now = time.time()
    tid = kb.create_task(conn, title="manual-pin", assignee="default")
    ended = int(now) - qf.WALL_RECENT_WINDOW_SECONDS - 60
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata)"
        " VALUES (?, 'default', 'completed', ?, ?, 'completed', '{}')",
        (tid, ended - 60, ended))
    conn.execute(
        "UPDATE tasks SET model_override='manual-model', provider_override='openai-codex' WHERE id = ?",
        (tid,))
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, NULL, 'model_override_set', ?, ?)",
        (tid, json.dumps({"model": "manual-model", "provider": "openai-codex"}), int(now)))
    conn.commit()

    task = kb.get_task(conn, tid)
    res = qf.claim_lane(conn, task, inject_now=now)
    assert res is None  # board model_override wins, no reroute
    row = conn.execute(
        "SELECT model_override, provider_override FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row["model_override"], row["provider_override"]) == ("manual-model", "openai-codex")
    # Object stayed coherent: the manual override is untouched everywhere.
    assert task.model_override == "manual-model"
    assert task.provider_override == "openai-codex"


# ---------------------------------------------------------------------------
# 3. eligibility through the real config path (D2 reviewer probe 1)
# ---------------------------------------------------------------------------

def test_unconfigured_provider_lane_is_ineligible_through_real_config(monkeypatch):
    """providers.merge block absent -> _lane_eligible returns False (D2 (i):
    provider existence is required; missing evidence fails closed)."""
    with patch(
        "hermes_cli.config.read_raw_config_readonly",
        lambda *a, **k: {}, create=True,
    ):
        assert qf._lane_eligible(
            {"model": "some/model", "provider": "merge"}, "openai-codex") is False


def test_allowlist_key_missing_fails_closed(monkeypatch):
    """Provider block present, explicit allowlist key missing -> the menu
    of models this provider serves is unproven; DESIGN §2 (i) says missing
    allowlist evidence fails closed even though the provider block exists
    (reviewer verdict 1 — replaces the old no-pin-passes expectation)."""
    with patch(
        "hermes_cli.config.read_raw_config_readonly",
        lambda *a, **k: {"providers": {"merge": {"base_url": "https://example.invalid"}}},
        create=True,
    ):
        assert qf._lane_eligible(
            {"model": "some/model", "provider": "merge"}, "openai-codex") is False


def test_allowlist_pin_respected_through_real_reader(monkeypatch):
    """Configured pin + lane model inside/outside it -> eligible/ineligible."""
    with patch(
        "hermes_cli.config.read_raw_config_readonly",
        lambda *a, **k: {"providers": {"merge": {"models": ["zai/glm-5.3-flash"]}}},
        create=True,
    ):
        assert qf._lane_eligible(
            {"model": "zai/glm-5.3-flash", "provider": "merge"}, "openai-codex") is True
        assert qf._lane_eligible(
            {"model": "not/pinned", "provider": "merge"}, "openai-codex") is False
