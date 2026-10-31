"""Tests for the quota-wall merge fallback (t_e7827d91).

Covers: wall detection from latest run, dispatch-grace fuel door telemetry
(works with the gate OFF so Packet 14 GAP E stays complete), gate precedence
(env > config, both off = static lane kept), board model_override precedence,
fallback lane selection skipping a provider that is itself walled, and
per-surfacing-run stall dedupe. In-memory sqlite rows mirror the dispatcher's
schemas (tasks / task_runs).
"""

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import hermes_cli.kanban_quota_fallback as qf


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, model_override TEXT, "
        "provider_override TEXT, assignee TEXT)"
    )
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, "
        "task TEXT, profile TEXT, status TEXT, outcome TEXT, started_at REAL, "
        "ended_at REAL, metadata TEXT, error TEXT, summary TEXT)"
    )
    yield conn
    conn.close()


class FakeTask:
    def __init__(self, id="t_1", model_override=None, provider_override=None):
        self.id = id
        self.model_override = model_override
        self.provider_override = provider_override
        self.assignee = "default"


def _seed_run(conn, task_id="t_1", outcome="rate_limited", ended_offset=60.0,
              model="gpt-6-luna", provider="openai-codex", now=None):
    now = now if now is not None else time.time()
    conn.execute(
        "INSERT INTO tasks (id, status, model_override) VALUES (?, 'ready', NULL)",
        (task_id,),
    )
    meta = json.dumps({"model": {"model": model, "provider": provider, "effort": "ultra"}})
    conn.execute(
        "INSERT INTO task_runs (task_id, task, profile, status, outcome, started_at, "
        "ended_at, metadata) VALUES (?, 'x', 'default', 'rate_limited', ?, ?, ?, ?)",
        (task_id, outcome, now - 120.0, now - ended_offset, meta),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Wall detection
# ---------------------------------------------------------------------------

class TestWalledProvider:
    def test_rate_limited_run_yields_provider(self, db, monkeypatch):
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        _seed_run(db)
        latest = qf.latest_run(db, "t_1")
        assert qf.walled_provider(latest, time.time()) == ("gpt-6-luna", "openai-codex")

    def test_non_rate_limited_run_is_not_a_wall(self, db):
        _seed_run(db, outcome="completed")
        assert qf.walled_provider(qf.latest_run(db, "t_1"), time.time()) is None

    def test_stale_wall_beyond_window_is_not_a_wall(self, db):
        _seed_run(db, ended_offset=qf.WALL_RECENT_WINDOW_SECONDS + 10)
        assert qf.walled_provider(qf.latest_run(db, "t_1"), time.time()) is None

    def test_undatable_ended_at_is_not_a_wall(self, db):
        now = time.time()
        _seed_run(db, now=now)
        db.execute("UPDATE task_runs SET ended_at = 'not-a-date'")
        db.commit()
        assert qf.walled_provider(qf.latest_run(db, "t_1"), now) is None

    def test_wall_without_provider_attribution_is_not_routable(self, db):
        now = time.time()
        _seed_run(db, now=now)
        db.execute("UPDATE task_runs SET metadata = '{}'")
        db.commit()
        assert qf.walled_provider(qf.latest_run(db, "t_1"), now) is None


# ---------------------------------------------------------------------------
# Fuel door (card ask #2): stall telemetry with the gate OFF
# ---------------------------------------------------------------------------

class TestFuelDoor:
    def test_walled_ready_card_with_zero_workers_logs_gap_e_stall(self, db, tmp_path, monkeypatch):
        ledger = tmp_path / "dispatch_stall.jsonl"
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        now = time.time()
        grace = qf.DISPATCH_GRACE_SECONDS + 5
        _seed_run(db, ended_offset=grace, now=now)

        lane = qf.claim_lane(db, FakeTask(), inject_now=now)
        assert lane is None  # gate off: static lane kept

        row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
        assert row["task_id"] == "t_1"
        assert row["reason"] == "primary_provider_quota_walled"
        assert row["active_workers"] == 0
        assert row["stall_signal"] == "ready_with_zero_workers_on_walled_provider"
        assert row["gap_e"] == "kanban_dispatch_stall"
        assert qf.DISPATCH_GRACE_SECONDS + 4 <= row["idle_seconds"] <= qf.DISPATCH_GRACE_SECONDS + 6

    def test_dashboards_read_the_signal_as_provider_walled(self, db, tmp_path, monkeypatch):
        """The evaluator contract: idle_seconds = staleness of the walled run."""
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        now = time.time()
        _seed_run(db, ended_offset=400.0, now=now)
        qf.claim_lane(db, FakeTask(), inject_now=now)
        row = json.loads((tmp_path / "dispatch_stall.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert 395.0 <= row["idle_seconds"] <= 405.0

    def test_running_workers_suppress_stall_signal(self, db, tmp_path, monkeypatch):
        """Card ask #2: the stall requires ZERO active workers. A walled card
        past grace while dispatch traffic is alive = first requeue tick of an
        understood cooldown, not a dispatch-stall outage."""
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        db.execute("INSERT INTO tasks (id, status) VALUES ('t_other', 'running')")
        assert qf.active_workers(db) == 1
        _seed_run(db, ended_offset=qf.DISPATCH_GRACE_SECONDS + 30)
        qf.claim_lane(db, FakeTask(), inject_now=time.time())
        assert not (tmp_path / "dispatch_stall.jsonl").exists()

    def test_unwalled_card_logs_nothing(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        _seed_run(db, outcome="completed")
        qf.claim_lane(db, FakeTask(), inject_now=time.time())
        assert not (tmp_path / "dispatch_stall.jsonl").exists()


# ---------------------------------------------------------------------------
# Gate precedence + fallback selection
# ---------------------------------------------------------------------------

class TestGateAndLanes:
    def test_config_flag_true_enables_gate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as hconfig
        monkeypatch.setattr(
            hconfig, "read_raw_config_readonly",
            lambda *a, **k: {"providers": {"merge": {"models": ["zai/glm-5.3-flash", "deepseek/deepseek-v4-flash-0731"]}, "routers": {"quota_fallback": {"enabled": True}}}},
            raising=False,
        )
        assert qf.fallback_enabled() is True

    def test_config_flag_off_keeps_gate_closed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as hconfig
        monkeypatch.setattr(
            hconfig, "read_raw_config_readonly",
            lambda *a, **k: {"providers": {"routers": {"quota_fallback": {"enabled": False}}}},
            raising=False,
        )
        assert qf.fallback_enabled() is False

    def test_unreadable_config_fails_safe_off(self, monkeypatch, tmp_path):
        """Fail-safe: an unreadable config keeps routing OFF (never a surprise
        re-route of live dispatch traffic)."""
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as hconfig
        monkeypatch.setattr(
            hconfig, "read_raw_config_readonly",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unreadable")),
            raising=False,
        )
        assert qf.fallback_enabled() is False

    def test_gate_off_keeps_static_lane_even_when_walled(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        _seed_run(db)
        assert qf.claim_lane(db, FakeTask(), inject_now=time.time()) is None

    def test_gate_on_walled_yields_configured_fallback(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {'providers': {'merge': {'models': ['zai/glm-5.3-flash', 'deepseek/deepseek-v4-flash-0731']}, 'routers': {'quota_fallback': {'enabled': True}}}},
            raising=False,
        )
        monkeypatch.setattr(qf, "_fallback_lanes", lambda: [{"model": "zai/glm-5.3-flash", "provider": "merge"}])
        _seed_run(db)
        lane = qf.claim_lane(db, FakeTask(), inject_now=time.time())
        assert lane == ("zai/glm-5.3-flash", "merge")

    def test_healthy_primary_keeps_static_lane(self, db, monkeypatch):
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {"providers": {"routers": {"quota_fallback": {"enabled": True}}}},
            raising=False,
        )
        monkeypatch.setattr(qf, "_fallback_lanes", lambda: [{"model": "x", "provider": "y"}])
        _seed_run(db, outcome="completed")
        assert qf.claim_lane(db, FakeTask(), inject_now=time.time()) is None

    def test_board_model_override_always_wins(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {"providers": {"routers": {"quota_fallback": {"enabled": True}}}},
            raising=False,
        )
        monkeypatch.setattr(qf, "_fallback_lanes", lambda: [{"model": "z", "provider": "w"}])
        _seed_run(db)
        task = FakeTask(model_override="board-picked")
        assert qf.claim_lane(db, task, inject_now=time.time()) is None

    def test_no_fallback_lane_configured_keeps_static_lane(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {"providers": {"routers": {"quota_fallback": {"enabled": True}}}},
            raising=False,
        )
        monkeypatch.setattr(qf, "_fallback_lanes", list)
        _seed_run(db)
        assert qf.claim_lane(db, FakeTask(), inject_now=time.time()) is None

    def test_reason_visible_after_call(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {"providers": {"routers": {"quota_fallback": {"enabled": True}}}},
            raising=False,
        )
        monkeypatch.setattr(qf, "_fallback_lanes", lambda: [{"model": "zai/glm-5.3-flash", "provider": "merge"}])
        _seed_run(db)
        qf.claim_lane(db, FakeTask(), inject_now=time.time())
        assert "fallback" in (qf.reason() or "")


# ---------------------------------------------------------------------------
# Cool-down loop guard + no-downgrade floor + default fleet
# ---------------------------------------------------------------------------

class TestCooldownAndFloor:
    def test_fallback_rerun_held_inside_cooldown(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {"providers": {"routers": {"quota_fallback": {"enabled": True}}}},
            raising=False,
        )
        monkeypatch.setattr(qf, "_fallback_lanes", lambda: [{"model": "zai/glm-5.3-flash", "provider": "merge"}])
        now = time.time()
        # Latest run = the fallback lane itself just ended rate_limited.
        _seed_run(db, model="zai/glm-5.3-flash", provider="merge", ended_offset=60.0, now=now)
        assert qf.claim_lane(db, FakeTask(), inject_now=now) is None
        assert qf.reason() == "fallback cooldown hold"

    def test_cooldown_release_chooses_next_lane_when_run_aged_out(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {'providers': {'routers': {'quota_fallback': {'enabled': True}}, 'merge': {'models': ['zai/glm-5.3-flash', 'deepseek/deepseek-v4-flash-0731']}, 'test-alt': {'base_url': 'https://alt.invalid'}}},
            raising=False,
        )
        monkeypatch.setattr(qf, "_fallback_lanes", lambda: [
            {"model": "zai/glm-5.3-flash", "provider": "merge"},
            {"model": "test/alt-model", "provider": "test-alt"},
        ])
        now = time.time()
        _seed_run(db, model="zai/glm-5.3-flash", provider="merge",
                  ended_offset=qf.FALLBACK_COOLDOWN_SECONDS + 60, now=now)
        assert qf.claim_lane(db, FakeTask(), inject_now=now) == ("test/alt-model", "test-alt")

    def test_no_downgrade_floor_forge_chief_nova(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {'providers': {'merge': {'models': ['zai/glm-5.3-flash', 'deepseek/deepseek-v4-flash-0731']}, 'routers': {'quota_fallback': {'enabled': True}}}},
            raising=False,
        )
        _seed_run(db)  # codex walled
        for who in ("forge", "chief", "nova"):
            task = FakeTask()
            task.assignee = who
            assert qf.claim_lane(db, task, inject_now=time.time()) is None
            assert "floor" in (qf.reason() or "")

    def test_default_lanes_served_when_config_absent(self, db, tmp_path, monkeypatch):
        import hermes_cli.config as hconfig

        def _no_config(*a, **k):
            raise RuntimeError("no config in test")

        monkeypatch.setattr(hconfig, "read_raw_config_readonly", _no_config, raising=False)
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {'providers': {'merge': {'models': ['zai/glm-5.3-flash', 'deepseek/deepseek-v4-flash-0731']}, 'routers': {'quota_fallback': {'enabled': True}}}},
            raising=False,
        )
        _seed_run(db, provider="openai-codex")
        lane = qf.claim_lane(db, FakeTask(), inject_now=time.time())
        assert lane == ("zai/glm-5.3-flash", "merge")  # first DEFAULT lane

    def test_config_declared_lanes_replace_defaults(self, db, tmp_path, monkeypatch):
        import hermes_cli.config as hconfig

        monkeypatch.setattr(
            hconfig, "read_raw_config_readonly",
            lambda *a, **k: {'providers': {'routers': {'quota_fallback': {'fallbacks': [{'model': 'c1', 'provider': 'p1'}]}}, 'p1': {'base_url': 'https://p1.invalid'}}},
            raising=False,
        )
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {'providers': {'routers': {'quota_fallback': {'enabled': True, 'fallbacks': [{'model': 'c1', 'provider': 'p1'}]}}, 'p1': {'base_url': 'https://p1.invalid'}}},
            raising=False,
        )
        _seed_run(db, provider="openai-codex")
        assert qf.claim_lane(db, FakeTask(), inject_now=time.time()) == ("c1", "p1")

    def test_lane_matching_walled_provider_is_skipped(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {'providers': {'merge': {'models': ['zai/glm-5.3-flash', 'deepseek/deepseek-v4-flash-0731']}, 'routers': {'quota_fallback': {'enabled': True}}}},
            raising=False,
        )
        monkeypatch.setattr(qf, "_fallback_lanes", lambda: [
            {"model": "codex-stub", "provider": "openai-codex"},
            {"model": "zai/glm-5.3-flash", "provider": "merge"},
        ])
        _seed_run(db, provider="openai-codex")
        assert qf.claim_lane(db, FakeTask(), inject_now=time.time()) == ("zai/glm-5.3-flash", "merge")




class TestRegressionTemplates:
    def test_junk_card_shape_does_not_trip_stall_telemetry(self, db, tmp_path, monkeypatch):
        """title='x' / assignee='a' junk template: non-profile assignee is
        skipped_nonspawnable upstream, so claim_lane never sees a wall."""
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        now = time.time()
        _seed_run(db, outcome="completed", now=now)
        task = FakeTask(id="t_76457bd0")
        task.assignee = "a"  # junk non-profile assignee
        assert qf.claim_lane(db, task, inject_now=now) is None
        assert not (tmp_path / "dispatch_stall.jsonl").exists()

    def test_stale_wall_does_not_re_trip_the_page(self, db, tmp_path, monkeypatch):
        """A wall older than the evidence window must not route or log — this is
        the strand-overnight shape from the 2026-10-22 alarm."""
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        import hermes_cli.config as _hconfig
        monkeypatch.setattr(
            _hconfig, "read_raw_config_readonly",
            lambda *a, **k: {"providers": {"routers": {"quota_fallback": {"enabled": True}}}},
            raising=False,
        )
        monkeypatch.setattr(qf, "_fallback_lanes", lambda: [{"model": "zai/glm-5.3-flash", "provider": "merge"}])
        _seed_run(db, ended_offset=qf.WALL_RECENT_WINDOW_SECONDS + 60)
        now = time.time()
        assert qf.claim_lane(db, FakeTask(), inject_now=now) is None
        assert not (tmp_path / "dispatch_stall.jsonl").exists()

    def test_repeat_guard_pass_dedupe_one_row_per_surfaced_run(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        now = time.time()
        _seed_run(db, ended_offset=qf.DISPATCH_GRACE_SECONDS + 10, now=now)
        qf.claim_lane(db, FakeTask(), inject_now=now)
        qf.claim_lane(db, FakeTask(), inject_now=now + 1)
        lines = (tmp_path / "dispatch_stall.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1  # same surfaced run -> exactly one stall row


# ---------------------------------------------------------------------------
# Ledger robustness
# ---------------------------------------------------------------------------

class TestLedgerRobustness:
    def test_corrupt_last_line_does_not_crash_dedupe(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(qf, "_ledger_dir", lambda: tmp_path)
        (tmp_path / "dispatch_stall.jsonl").write_text("not json\n", encoding="utf-8")
        monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
        _seed_run(db)
        # dedupe returns True (can't prove not-logged) — telemetry skipped, no raise
        assert qf._stall_dedupe(db, "t_1", "k") is True

    def test_missing_run_rows_return_none(self, db):
        assert qf.latest_run(db, "t_missing") is None
