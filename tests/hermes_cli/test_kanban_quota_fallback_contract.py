"""DESIGN.md contract tests added by split 2/3 (t_27cf3f73).

Covers the three gaps the recovered commit (bd9c8c01670) left vs DESIGN.md:
- D1.3b: static-lane attribution for unstamped rate_limited runs;
- D2: lane eligibility guard (allowlist pin + jev/grok exclusion + rollover);
- D3: persistent override write, newest-event-wins ordering, guarded cleanup.

Fixtures mirror the recovered test file (in-memory sqlite, FakeTask).
"""

import json
import sqlite3
import time
from pathlib import Path

import pytest

import hermes_cli.kanban_quota_fallback as qf


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    """Unfenced board home (canonical harness pattern from
    test_kanban_core_functionality): HERMES_HOME + Path.home point inside
    tmp_path, so kanban_home() is not under the delegate-child fence marker
    and write_txn-based D3 helpers can exercise their real write path on
    in-memory conns."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The harness Environment carries the worker's delegated-child fence
    # marker (HERMES_DELEGATED_CHILD_CONTEXT); D3 writes must be exercisable
    # on the fixture conn, so the fixture scope is unfenced.
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    return home


@pytest.fixture()
def db(kanban_home):
    # isolation_level=None (autocommit): the real dispatcher conn runs with
    # explicit write_txn boundaries; implicit-per-execute transactions would
    # leave the conn in_transaction=True and break the D3 write_txn helpers.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, model_override TEXT, "
        "provider_override TEXT, assignee TEXT, current_run_id INTEGER)"
    )
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, "
        "task TEXT, profile TEXT, status TEXT, outcome TEXT, started_at REAL, "
        "ended_at REAL, metadata TEXT, error TEXT, summary TEXT)"
    )
    conn.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, "
        "run_id INTEGER, kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL)"
    )
    yield conn
    conn.close()


class FakeTask:
    def __init__(self, id="t_1", model_override=None, provider_override=None, assignee="default"):
        self.id = id
        self.model_override = model_override
        self.provider_override = provider_override
        self.assignee = assignee


def _seed_run(conn, task_id="t_1", outcome="rate_limited", ended_offset=60.0,
              model="gpt-6-luna", provider="openai-codex", now=None, stamped=True, insert_task=True):
    now = now if now is not None else time.time()
    meta = json.dumps({"model": {"model": model, "provider": provider, "effort": "ultra"}}) if stamped else "{}"
    if insert_task:
        conn.execute(
            "INSERT INTO tasks (id, status, model_override) VALUES (?, 'ready', NULL)",
            (task_id,),
        )
    conn.execute(
        "INSERT INTO task_runs (task_id, task, profile, status, outcome, started_at, "
        "ended_at, metadata) VALUES (?, 'x', 'default', 'rate_limited', ?, ?, ?, ?)",
        (task_id, outcome, now - 120.0, now - ended_offset, meta),
    )
    conn.commit()


def _enable_gate(monkeypatch):
    import hermes_cli.config as _hconfig
    monkeypatch.setattr(
        _hconfig, "read_raw_config_readonly",
        lambda *a, **k: {"providers": {"routers": {"quota_fallback": {"enabled": True}}}},
        raising=False,
    )


# ---------------------------------------------------------------------------
# D1.3b: static-lane attribution
# ---------------------------------------------------------------------------

class TestStaticLaneAttribution:
    def test_unstamped_run_attributed_to_static_lane(self, db, tmp_path, monkeypatch):
        """Unstamped rate_limited run + resolvable static lane -> wall."""
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        monkeypatch.setattr(qf, "_static_lane_for_assignee", lambda a: ("gpt-6.1-sol", "openai-codex"))
        _seed_run(db, stamped=False, ended_offset=qf.DISPATCH_GRACE_SECONDS + 30)
        now = time.time()
        assert qf.claim_lane(db, FakeTask(), inject_now=now) is None  # gate off = no route
        # but the wall WAS detected: the fuel door logged it
        assert (tmp_path / "dispatch_stall.jsonl").read_text(encoding="utf-8").count("gpt-6.1-sol") >= 1

    def test_unstamped_run_without_static_lane_routes_nothing(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        monkeypatch.setattr(qf, "_static_lane_for_assignee", lambda a: (None, None))
        _seed_run(db, stamped=False)
        qf.claim_lane(db, FakeTask(), inject_now=time.time())
        assert not (tmp_path / "dispatch_stall.jsonl").exists()  # no attribution = not even telemetry

    def test_stamped_attribution_outranks_static_lane(self, db, tmp_path, monkeypatch):
        """D1.3a stamp wins when present; static lane only fills the unstamped gap."""
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        monkeypatch.setattr(qf, "_static_lane_for_assignee", lambda a: ("wrong", "wrong-p"))
        _seed_run(db, stamped=True, model="gpt-6-luna", provider="openai-codex",
                  ended_offset=qf.DISPATCH_GRACE_SECONDS + 30)
        qf.claim_lane(db, FakeTask(), inject_now=time.time())
        text = (tmp_path / "dispatch_stall.jsonl").read_text(encoding="utf-8")
        assert "gpt-6-luna" in text and "wrong" not in text

    def test_static_lane_reader_real_profile(self, monkeypatch):
        """_static_lane_for_assignee resolves through the real profiles helper
        (no mocking) and fails closed for unknown profiles."""
        monkeypatch.setattr(
            "hermes_cli.profiles.resolve_profile_env",
            lambda name: r"C:\nonexistent\profiles\definitely-not-here",
            raising=False,
        )
        assert qf._static_lane_for_assignee("someone") == (None, None)
        assert qf._static_lane_for_assignee("") == (None, None)


# ---------------------------------------------------------------------------
# D2: eligibility guard
# ---------------------------------------------------------------------------

class TestLaneEligibility:
    def test_jev_lane_is_never_eligible(self):
        assert qf._lane_eligible(
            {"model": "typesafe/jev-1.13", "provider": "merge"}, "openai-codex") is False

    def test_grok_lane_is_never_eligible(self):
        assert qf._lane_eligible(
            {"model": "xai/grok-4.5", "provider": "merge"}, "openai-codex") is False

    def test_merge_lane_outside_allowlist_is_not_eligible(self, monkeypatch):
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {"providers": {"merge": {"models": ["zai/glm-5.3-flash", "typesafe/jev-1.13"]}}},
            raising=False,
        )
        assert qf._lane_eligible(
            {"model": "not-in-allowlist/model", "provider": "merge"}, "openai-codex") is False
        assert qf._lane_eligible(
            {"model": "zai/glm-5.3-flash", "provider": "merge"}, "openai-codex") is True

    def test_empty_default_allowlist_treated_as_no_pin(self, monkeypatch):
        """An absent/empty providers.merge.models block = no pin evidence in
        config; the guard declines to enforce a pin it cannot see."""
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {"providers": {"merge": {}}},
            raising=False,
        )
        assert qf._lane_eligible(
            {"model": "anything/here", "provider": "merge"}, "walled") is True

    def test_unreadable_allowlist_fails_closed(self, monkeypatch):
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
            raising=False,
        )
        assert qf._lane_eligible(
            {"model": "anything/here", "provider": "merge"}, "walled") is False

    def test_walled_provider_lane_is_skipped_in_pick(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        _enable_gate(monkeypatch)
        monkeypatch.setattr(qf, "_fallback_lanes", lambda: [
            {"model": "codex-stub", "provider": "openai-codex"},
            {"model": "zai/glm-5.3-flash", "provider": "merge"},
        ])
        _seed_run(db, provider="openai-codex")
        lane = qf.claim_lane(db, FakeTask(), inject_now=time.time())
        assert lane == ("zai/glm-5.3-flash", "merge")

    def test_all_lanes_ineligible_keeps_static_lane(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        _enable_gate(monkeypatch)
        monkeypatch.setattr(qf, "_fallback_lanes", lambda: [
            {"model": "typesafe/jev-1.13", "provider": "merge"},  # excluded model
        ])
        _seed_run(db, provider="openai-codex")
        assert qf.claim_lane(db, FakeTask(), inject_now=time.time()) is None
        assert qf.reason() == "no eligible fallback lane configured"


# ---------------------------------------------------------------------------
# D3: persist + ordering + cleanup
# ---------------------------------------------------------------------------

class TestPersistAndCleanup:
    def test_persist_writes_row_and_full_event(self, db, monkeypatch):
        db.execute(
            "INSERT INTO tasks (id, status, model_override, current_run_id) "
            "VALUES ('t_1', 'running', NULL, 7)"
        )
        ok = qf._persist_fallback_override(db, "t_1", "zai/glm-5.3-flash", "merge",
                                           walled=("gpt-6-luna", "openai-codex"))
        assert ok is True
        row = db.execute(
            "SELECT model_override, provider_override FROM tasks WHERE id = 't_1'").fetchone()
        assert (row["model_override"], row["provider_override"]) == ("zai/glm-5.3-flash", "merge")
        ev = db.execute(
            "SELECT kind, payload FROM task_events ORDER BY id DESC LIMIT 1").fetchone()
        assert ev["kind"] == "quota_fallback_lane_picked"
        payload = json.loads(ev["payload"])
        assert payload["model"] == "zai/glm-5.3-flash"
        assert payload["provider"] == "merge"
        assert payload["run_id"] == 7
        assert payload["walled_model"] == "gpt-6-luna"
        assert payload["walled_provider"] == "openai-codex"
        assert payload["source"] == "dispatch.quota_fallback"

    def test_newest_event_wins_over_manual_override(self, db, monkeypatch):
        """D3 ordering: a manual model_override_set AFTER a fallback pick
        must satisfy 'manual writes are respected' via newest-wins queries."""
        now = int(time.time())
        db.execute(
            "INSERT INTO tasks (id, status, model_override, current_run_id) "
            "VALUES ('t_1', 'running', NULL, 7)")
        qf._persist_fallback_override(db, "t_1", "zai/glm-5.3-flash", "merge")
        db.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES ('t_1', 7, 'model_override_set', ?, ?)",
            (json.dumps({"model": "manual-model", "provider": "manual-p"}), now + 10))
        kind, payload = qf._newest_override_event(db, "t_1")
        assert kind == "model_override_set"
        assert payload["model"] == "manual-model"

    def test_walled_run_current_blocks_cleanup(self, db, monkeypatch):
        now = time.time()
        db.execute("INSERT INTO tasks (id, status, model_override, provider_override) VALUES ('t_1', 'ready', 'fb-model', 'fb-p')")
        db.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES ('t_1', NULL, 'quota_fallback_lane_picked', ?, ?)",
            (json.dumps({"model": "fb-model", "provider": "fb-p"}), now))
        _seed_run(db, ended_offset=60.0, provider="openai-codex", now=now, insert_task=False)  # fresh walled run
        assert qf._walled_run_current(db, "t_1", now=now) is True
        assert qf._cleanup_fallback_override(db, "t_1") is False
        row = db.execute("SELECT model_override FROM tasks WHERE id='t_1'").fetchone()
        assert row["model_override"] == "fb-model"  # kept: wall is current evidence

    def test_aged_out_wall_with_clean_latest_run_cleans(self, db, monkeypatch):
        now = time.time()
        db.execute("INSERT INTO tasks (id, status, model_override, provider_override) VALUES ('t_1', 'ready', 'fb-model', 'fb-p')")
        db.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES ('t_1', NULL, 'quota_fallback_lane_picked', ?, ?)",
            (json.dumps({"model": "fb-model", "provider": "fb-p"}), int(now)))
        _seed_run(db, outcome="completed", ended_offset=qf.WALL_RECENT_WINDOW_SECONDS + 60, now=now, insert_task=False)
        assert qf._cleanup_fallback_override(db, "t_1") is True
        row = db.execute("SELECT model_override FROM tasks WHERE id='t_1'").fetchone()
        assert row["model_override"] is None
        ev = db.execute(
            "SELECT kind FROM task_events ORDER BY id DESC LIMIT 1").fetchone()
        assert ev["kind"] == "quota_fallback_lane_cleared"

    def test_manual_override_never_cleaned(self, db, monkeypatch):
        now = int(time.time())
        db.execute("INSERT INTO tasks (id, status, model_override, provider_override) VALUES ('t_1', 'ready', 'manual-model', 'manual-p')")
        db.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES ('t_1', NULL, 'model_override_set', ?, ?)",
            (json.dumps({"model": "manual-model", "provider": "manual-p"}), now))
        _seed_run(db, outcome="completed", ended_offset=qf.WALL_RECENT_WINDOW_SECONDS + 60, insert_task=False)
        assert qf._cleanup_fallback_override(db, "t_1") is False
        row = db.execute("SELECT model_override FROM tasks WHERE id='t_1'").fetchone()
        assert row["model_override"] == "manual-model"

    def test_uneleventh_payload_mismatch_blocks_cleanup(self, db, monkeypatch):
        now = int(time.time())
        db.execute("INSERT INTO tasks (id, status, model_override) VALUES ('t_1', 'ready', 'someone-elses')")
        db.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES ('t_1', NULL, 'quota_fallback_lane_picked', ?, ?)",
            (json.dumps({"model": "fb-model", "provider": "fb-p"}), now))
        assert qf._cleanup_fallback_override(db, "t_1") is False

    def test_opportunistic_cleanup_runs_inside_claim_lane(self, db, tmp_path, monkeypatch):
        """When the gate is ON and the contract holds, claim_lane sweeps the
        stale override as part of the claim pass."""
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        _enable_gate(monkeypatch)
        now = time.time()
        db.execute("INSERT INTO tasks (id, status, model_override, provider_override) VALUES ('t_1', 'ready', 'fb-model', 'fb-p')")
        db.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES ('t_1', NULL, 'quota_fallback_lane_picked', ?, ?)",
            (json.dumps({"model": "fb-model", "provider": "fb-p"}), int(now)))
        _seed_run(db, outcome="completed", ended_offset=qf.WALL_RECENT_WINDOW_SECONDS + 60, now=now, insert_task=False)
        qf.claim_lane(db, FakeTask(), inject_now=now)
        row = db.execute("SELECT model_override FROM tasks WHERE id='t_1'").fetchone()
        assert row["model_override"] is None

    def test_gate_off_never_cleans(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        now = time.time()
        db.execute("INSERT INTO tasks (id, status, model_override, provider_override) VALUES ('t_1', 'ready', 'fb-model', 'fb-p')")
        db.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES ('t_1', NULL, 'quota_fallback_lane_picked', ?, ?)",
            (json.dumps({"model": "fb-model", "provider": "fb-p"}), int(now)))
        _seed_run(db, outcome="completed", ended_offset=qf.WALL_RECENT_WINDOW_SECONDS + 60, now=now, insert_task=False)
        qf.claim_lane(db, FakeTask(), inject_now=now)
        row = db.execute("SELECT model_override FROM tasks WHERE id='t_1'").fetchone()
        assert row["model_override"] == "fb-model"  # dormant routing = dormant cleanup


# ---------------------------------------------------------------------------
# Full-loop: gate-on wall -> fallback pick through the real claim flow
# ---------------------------------------------------------------------------

class TestFullLoop:
    def test_board_override_wins_and_persist_skipped(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        _enable_gate(monkeypatch)
        monkeypatch.setattr(qf, "_fallback_lanes", lambda: [{"model": "zai/glm-5.3-flash", "provider": "merge"}])
        _seed_run(db, provider="openai-codex")
        db.execute("UPDATE tasks SET model_override = 'board-picked' WHERE id = 't_1'")
        task = FakeTask(model_override="board-picked")
        assert qf.claim_lane(db, task, inject_now=time.time()) is None
        # board override untouched
        row = db.execute("SELECT model_override FROM tasks WHERE id='t_1'").fetchone()
        assert row["model_override"] == "board-picked"

    def test_no_downgrade_floor_never_rerouted(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        _enable_gate(monkeypatch)
        _seed_run(db, provider="openai-codex")
        for who in ("forge", "chief", "nova"):
            task = FakeTask(assignee=who)
            assert qf.claim_lane(db, task, inject_now=time.time()) is None
            assert "floor" in (qf.reason() or "")
