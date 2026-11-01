"""D4 post-claim worker accounting (t_4a3a9f4e); no production I/O.

Reuse the accepted real-schema dispatch harness without modifying the
concurrently owned fuel-door consumer tests.
"""

import json

import pytest

import hermes_cli.kanban_quota_fallback as qf
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli.test_kanban_quota_fallback_seam import (
    _lane_ready,
    _seed_walled_run,
    conn,
    kanban_home,
    quota_env,
)


def _dispatch(conn, tid):
    return kbd._dispatch_lane_task(
        conn, conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone(),
        "default", kbd.DispatchResult(), lane="ready", dry_run=False,
        ttl_seconds=600, board=None, failure_limit=2,
        spawn_fn=lambda task, workspace, board=None: 424242,
        per_profile_cap=None, per_profile_running={},
    )


@pytest.mark.parametrize("age,other_count,emits", [
    (600, 0, True),
    (600, 1, False),
    (600, 2, False),
    (179, 0, False),
    (180, 0, False),
    (181, 0, True),
    (899, 0, True),
    (900, 0, False),
    (901, 0, False),
])
def test_real_seam_counts_only_other_workers_and_keeps_d4_boundaries(
    conn, quota_env, monkeypatch, all_assignees_spawnable, age, other_count, emits,
):
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    monkeypatch.setattr(qf, "fallback_enabled", lambda: False)
    tid = kb.create_task(conn, title="p9 D4 producer fixture", assignee="default", priority=9)
    _seed_walled_run(conn, tid, ended_offset=age)
    _lane_ready(conn, tid)
    ended = qf.latest_run(conn, tid)["ended_at"]
    now = ended + age  # exact boundary, independent of wall-clock test duration
    for _ in range(other_count):
        other = kb.create_task(conn, title="other live worker", assignee="default")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
    conn.commit()
    assert qf.active_workers(conn) == other_count
    real_claim_lane = qf.claim_lane
    observed = {}

    def trace_claim_lane(c, task, **kwargs):
        observed["status"] = c.execute(
            "SELECT status FROM tasks WHERE id=?", (task.id,),
        ).fetchone()[0]
        observed["total"] = qf.active_workers(c)
        return real_claim_lane(c, task, inject_now=now, **kwargs)

    monkeypatch.setattr(qf, "claim_lane", trace_claim_lane)
    assert _dispatch(conn, tid)
    assert observed == {"status": "running", "total": other_count + 1}
    task = kb.get_task(conn, tid)
    assert task.model_override is None and task.provider_override is None
    assert not conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='quota_fallback_lane_picked'",
        (tid,),
    ).fetchone()
    ledger = quota_env / "dispatch_stall.jsonl"
    if not emits:
        assert not ledger.exists()
        return
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert rows == [{
        "ts": now, "task_id": tid, "run_key": str(ended), "lane": "ready",
        "reason": "primary_provider_quota_walled", "walled_model": "gpt-6-luna",
        "walled_provider": "openai-codex", "active_workers": 0,
        "idle_seconds": float(age),
        "stall_signal": "ready_with_zero_workers_on_walled_provider",
        "gap_e": "kanban_dispatch_stall",
    }]
    # Same ended run, called again after claiming: exactly one surfaced row.
    assert qf.claim_lane(conn, task, lane="ready") is None
    assert ledger.read_text(encoding="utf-8").splitlines() == [json.dumps(rows[0])]


@pytest.mark.parametrize("candidate_status", ["ready", "running", "review"])
def test_worker_exclusion_is_identity_specific_not_unconditional_subtraction(conn, candidate_status):
    tid = kb.create_task(conn, title="candidate", assignee="default")
    other = kb.create_task(conn, title="same-profile other worker", assignee="default")
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (candidate_status, tid))
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
    conn.commit()
    assert qf.active_workers(conn) == 1 + (candidate_status == "running")
    assert qf.active_workers(conn, exclude_task_id=tid) == 1
    assert qf.active_workers(conn, exclude_task_id="absent-candidate") == qf.active_workers(conn)
