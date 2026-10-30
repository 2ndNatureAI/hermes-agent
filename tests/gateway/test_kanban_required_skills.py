"""Unavailable review skills settle as an actionable, claim-fenced block, never unknown."""
import json
import os
from types import SimpleNamespace

import pytest

from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_state_runtime import RuntimeStoreError


@pytest.mark.parametrize("mode", ["missing", "disabled", "partial"])
@pytest.mark.parametrize("stale", [False, True])
def test_required_skill_failure_blocks_exact_review_attempt(tmp_path, monkeypatch, mode, stale):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # A real profile skill loader: missing and explicitly disabled are both dependencies.
    if mode != "missing":
        skill = tmp_path / "skills" / ("available" if mode == "partial" else "review-contract")
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {skill.name}\ndescription: review\n---\nReview the artifact.\n", encoding="utf-8")
        if mode == "disabled":
            (tmp_path / "config.yaml").write_text(
                "skills:\n  disabled: [review-contract]\n", encoding="utf-8")
    kb.init_db()
    path = kb.kanban_db_path()
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title="review contract", assignee="default")
        assert kb.request_review(conn, task_id, summary="artifact ready", reviewer="default")
        task = kb.claim_review_task(conn, task_id, claimer="host:owner")
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "worker_bound",
                             {"pid": os.getpid(), "claim_lock": task.claim_lock}, run_id=task.current_run_id)
    context = {"db": str(path), "task_id": task_id, "run_id": task.current_run_id,
               "claim_lock": task.claim_lock, "skills": ["review-contract"], "profile": "default",
               "context": "", "goal_mode": False}
    if mode == "partial":
        context["skills"].append("available")
    if stale:
        context["claim_lock"] = "foreign-owner"
    agent = SimpleNamespace(session_id="review-preflight")
    frame = {"text": "work", "policy": {"kanban_json": json.dumps(context)}}

    if stale:
        with pytest.raises(RuntimeStoreError, match="stale_kanban_claim"):
            session_kanban._run_worker_turns(agent, frame, [])
        with connect_closing(path) as conn:
            current = kb.get_task(conn, task_id)
            assert current.status == "running" and current.claim_lock == task.claim_lock
        return
    result = session_kanban._run_worker_turns(agent, frame, [])

    assert result["failure_reason"] == "kanban_skills_unavailable"
    assert result["failed"] is True
    assert "review-contract" in result["final_response"]
    assert "default" in result["final_response"] and "Next action:" in result["final_response"]
    with connect_closing(path) as conn:
        blocked = kb.get_task(conn, task_id)
        run = conn.execute("SELECT * FROM task_runs WHERE id=?", (context["run_id"],)).fetchone()
        assert blocked.status == "blocked" and blocked.block_kind == "needs_input"
        assert blocked.consecutive_failures == 0
        assert run["status"] == "blocked" and "review-contract" in run["summary"]
        assert kb.unblock_task(conn, task_id)
        assert kb.get_task(conn, task_id).status == "review"
    assert session_kanban.worker_exit_code(path, context) == 1
