"""Run-fields telemetry tests (t_0bdbd134): resolved-model + usage + quality
stamps on task_runs metadata.

Covers the 2026-09-22 repair of the 9/20 run-fields convention:
  - _publish_resolved_state (flat dict) must meet _auto_stamp_run_fields' reader
  - the live agent ref freezes token_usage at kanban_complete time
  - goal-judge done verdicts stamp a quality blob (no extra judge call)
  - reviewer changes_requested/approved stamp the implementer's run
  - quality stamps are absent-only (a worker self-report is never overwritten)
"""
from __future__ import annotations

import json
import os
from pathlib import Path as _Path

import pytest


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Same shape as tests/tools/test_kanban_tools.py::worker_env."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="run-fields-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


@pytest.fixture
def goal_env(worker_env, monkeypatch, tmp_path):
    """A claimed goal_mode task, like _make_goal_mode_worker_env in
    test_kanban_tools.py: created with goal_mode=True from the start."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="goal-run-fields-test",
                             body="Must achieve X with verified evidence.",
                             assignee="test-worker", goal_mode=True)
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _run_metadata_for(tid: str) -> dict:
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        rows = conn.execute(
            "SELECT id, metadata FROM task_runs WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        return {r["id"]: (json.loads(r["metadata"]) if r["metadata"] else {}) for r in rows}
    finally:
        conn.close()


class _FakeAgent:
    session_input_tokens = 100
    session_output_tokens = 50
    session_total_tokens = 150
    session_api_calls = 2
    session_estimated_cost_usd = 0.001


def test_auto_stamp_run_fields_flat_state_and_live_agent():
    """The publisher's flat resolved-state dict must land as resolved_model,
    and the live agent ref must freeze a token_usage snapshot."""
    from tools import kanban_tools as kt

    metadata: dict = {}
    kt._auto_stamp_run_fields(
        metadata,
        {"model": "zai/glm-5.3-flash", "provider": "merge", "base_url": "",
         "reasoning_effort": "ultra", "is_fallback": False},
        agent_ref=_FakeAgent(),
    )
    assert metadata["resolved_model"] == {
        "model": "zai/glm-5.3-flash", "provider": "merge", "reasoning_effort": "ultra",
    }
    assert metadata["token_usage"] == {
        "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
        "api_calls": 2, "cost_usd": 0.001,
    }


def test_auto_stamp_run_fields_nested_state_still_supported():
    """Back-compat: the nested {"resolved": ...} shape from the 9/20 commit
    still stamps (in case any caller passes it)."""
    from tools import kanban_tools as kt
    metadata: dict = {}
    kt._auto_stamp_run_fields(
        metadata, {"resolved": {"model": "m", "provider": "p"}})
    assert metadata["resolved_model"]["model"] == "m"
    assert metadata["resolved_model"]["provider"] == "p"


def test_auto_stamp_run_fields_no_state_is_noop():
    from tools import kanban_tools as kt
    metadata = {"keep": 1}
    kt._auto_stamp_run_fields(metadata, None)
    assert metadata == {"keep": 1}


def test_complete_stamps_usage(worker_env):
    """A worker completing with a live agent ref carries token_usage and the
    resolved route onto its run."""
    from tools import kanban_tools as kt

    out = kt._handle_complete(
        {"summary": "done with telemetry"},
        agent_state={"model": "test-model", "provider": "test", "reasoning_effort": "high"},
        agent_ref=_FakeAgent(),
    )
    d = json.loads(out)
    assert d.get("ok") is True, out
    meta = _run_metadata_for(worker_env)
    blob = next(iter(meta.values()))
    assert blob.get("resolved_model", {}).get("model") == "test-model"
    assert blob.get("token_usage", {}).get("total_tokens") == 150


def test_goal_judge_done_verdict_stamps_quality(goal_env, monkeypatch):
    """A goal-mode card whose judge says done gets a goal_judge quality blob
    on its active run — no second judge call."""
    import tools.kanban_tools as kt

    monkeypatch.setattr(kt, "_goal_judge_available", lambda: True)
    monkeypatch.setattr(
        kt, "judge_goal",
        lambda goal, last_response: ("done", "acceptance met", False, None, False))

    out = kt._handle_complete({"summary": "goal satisfied"})
    d = json.loads(out)
    assert d.get("ok") is True, out
    meta = _run_metadata_for(goal_env)
    blobs = [m for m in meta.values() if m.get("quality")]
    assert blobs, f"no quality stamp landed: {meta}"
    q = blobs[-1]["quality"]
    assert q["set_by"] == "goal_judge"
    assert q["verdict"] == "done"


def test_goal_judge_not_done_does_not_stamp(goal_env, monkeypatch):
    """A continue verdict must gate the handoff AND leave no quality stamp."""
    import tools.kanban_tools as kt

    monkeypatch.setattr(kt, "_goal_judge_available", lambda: True)
    monkeypatch.setattr(
        kt, "judge_goal",
        lambda goal, last_response: ("continue", "not finished", False, None, False))

    out = kt._handle_complete({"summary": "premature"})
    d = json.loads(out)
    assert d.get("ok") is not True, out
    meta = _run_metadata_for(goal_env)
    assert not [m for m in meta.values() if m.get("quality")], meta


def test_quality_stamp_absent_only(worker_env):
    """_stamp_quality_on_run must never overwrite an existing quality blob."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        run_id = kb._current_run_id(conn, worker_env)
        kt._stamp_quality_on_run(conn, worker_env, {"set_by": "worker", "verdict": "self"})
        kt._stamp_quality_on_run(conn, worker_env, {"set_by": "reviewer", "verdict": "approved"})
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        blob = json.loads(row["metadata"])
        assert blob["quality"]["set_by"] == "worker"
    finally:
        conn.close()


def test_goal_gate_returns_blob_shape():
    """_quality_from_judge maps a verdict to the documented blob shape."""
    from tools import kanban_tools as kt
    blob = kt._quality_from_judge("done", "acceptance met")
    assert blob == {"set_by": "goal_judge", "verdict": "done", "reason": "acceptance met"}
    assert kt._quality_from_judge("", "x") is None
