"""Tests for #scheduler-restart-unknown: shutdown-time durable ledger closure.

Covers the uncommitted cron/scheduler.py patch that registers each in-process
fire's durable ``execution_id`` beside the fire-owner registry and closes that
row inside ``mark_running_jobs_interrupted`` (via
``_finalize_interrupted_execution``) so it reaches a terminal state BEFORE the
owning process exits - instead of staying ``running`` until a later process's
dead-owner sweep files it as an unverifiable ``unknown`` ("Scheduler restarted
after this execution's owner exited before a durable terminal state").

Guarantees under test:
  1. an in-process fire's row is terminalized (failed) with the interruption
     reason at shutdown;
  2. the ledger closure must not depend on the jobs.json write succeeding;
  3. no closure for a token whose registered id is None/absent (external or
     restart-safe handoff): the row stays untouched, the adopting worker
     closes it;
  4. the owner-fencing is real: a row whose ledger fingerprint belongs to a
     DIFFERENT process is never rewritten from this process;
  5. a ledger failure at the closure call site is contained and must not
     block other jobs' shutdown marking.
"""

import sqlite3
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    """Module-level scheduler globals are shared across the test process."""
    import cron.scheduler as sched

    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()
    sched._RUNNING_EXECUTION_IDS.clear()
    yield
    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()
    sched._RUNNING_EXECUTION_IDS.clear()


@pytest.fixture()
def isolated_ledger(tmp_path, monkeypatch):
    """Point the execution ledger at a private per-test db file."""
    from cron import executions

    db = tmp_path / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", db)
    return executions


def _register_in_process_fire(job_id: str, execution_id: str) -> object:
    """Mirror run_one_job's registration exactly (the in-process lane)."""
    import cron.scheduler as sched

    profile_home = sched._get_hermes_home().resolve()
    key = sched._inflight_key(job_id)
    token = object()
    with sched._running_lock:
        sched._running_job_ids.add(key)
        sched._running_fire_owners.setdefault(key, {})[token] = (
            "fire-owner-1", profile_home)
        sched._running_execution_ids[token] = execution_id
    return token


class TestShutdownFinalizesExecutionLedger:

    def test_in_process_row_reaches_durable_terminal_state(
        self, isolated_ledger,
    ):
        import cron.scheduler as sched

        record = isolated_ledger.create_execution("job-shut", source="direct")
        exec_id = record["id"]
        assert record["status"] == "claimed"
        _register_in_process_fire("job-shut", exec_id)

        with patch("cron.scheduler.mark_job_run", return_value=True):
            marked = sched.mark_running_jobs_interrupted("gateway shutdown (test)")

        assert marked == ["job-shut"]
        row = isolated_ledger.get_execution(exec_id)
        assert row["status"] == "failed"  # durable terminal state, not running
        assert row["finished_at"] is not None
        assert "shutdown" in (row["error"] or "")

    def test_mark_job_run_failure_still_closes_ledger_row(self, isolated_ledger):
        """The ledger closure must not depend on the jobs.json write succeeding."""
        import cron.scheduler as sched

        record = isolated_ledger.create_execution("job-jjson", source="direct")
        exec_id = record["id"]
        _register_in_process_fire("job-jjson", exec_id)

        def _boom(*_a, **_k):
            raise OSError("disk full")

        with patch("cron.scheduler.mark_job_run", side_effect=_boom):
            marked = sched.mark_running_jobs_interrupted("gateway shutdown (test)")

        assert marked == []  # jobs.json path failed, pre-existing behavior
        row = isolated_ledger.get_execution(exec_id)
        assert row["status"] == "failed"  # ledger still closed durably
        assert row["finished_at"] is not None

    def test_unregistered_token_closes_nothing(self, isolated_ledger):
        """A token without a registered execution id (external/restart-safe
        handoff, or a fire that has not reached _run_one_job_body yet): nothing
        may be closed from this process - the adopting worker owns its row."""
        import cron.scheduler as sched

        record = isolated_ledger.create_execution("job-handoff", source="direct")
        exec_id = record["id"]
        _register_in_process_fire("job-handoff", exec_id)
        # Simulate the external-worker lane: clear the durable id registration
        # (run_one_job leaves the map entry absent when the external worker wins).
        with sched._running_lock:
            for token in list(sched._running_execution_ids):
                del sched._running_execution_ids[token]

        with patch("cron.scheduler.mark_job_run", return_value=True):
            sched.mark_running_jobs_interrupted("gateway shutdown (test)")

        row = isolated_ledger.get_execution(exec_id)
        assert row["status"] == "claimed"  # untouched - the worker owns it

    def test_owner_fencing_never_rewrites_another_process_row(
        self, isolated_ledger, tmp_path,
    ):
        """A row whose ledger fingerprint belongs to a different process (a
        worker that adopted the claim) is never rewritten from this process."""
        import cron.scheduler as sched

        record = isolated_ledger.create_execution("job-adopted", source="direct")
        exec_id = record["id"]
        _register_in_process_fire("job-adopted", exec_id)

        db_path = tmp_path / "executions.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE executions SET process_id='other-process', pid=999999 "
                "WHERE id=?", (exec_id,))
            conn.commit()
        finally:
            conn.close()

        with patch("cron.scheduler.mark_job_run", return_value=True):
            sched.mark_running_jobs_interrupted("gateway shutdown (test)")

        row = isolated_ledger.get_execution(exec_id)
        assert row["status"] == "claimed"  # adoptee row preserved

    def test_ledger_closure_failure_does_not_block_other_jobs(
        self, isolated_ledger,
    ):
        """One job's ledger closure blowing up must not block the rest of the
        shutdown marking: the call-site suppression covers it, and the next
        process's dead-owner sweep remains the fallback."""
        import cron.scheduler as sched

        r1 = isolated_ledger.create_execution("job-broken-ledger", source="direct")
        r2 = isolated_ledger.create_execution("job-healthy", source="direct")
        _register_in_process_fire("job-broken-ledger", r1["id"])
        _register_in_process_fire("job-healthy", r2["id"])

        def _finish_boom(*_a, **_k):
            raise OSError("ledger unavailable")

        with patch(
            "cron.executions.finish_execution",
            side_effect=_finish_boom,
        ):
            with patch("cron.scheduler.mark_job_run", return_value=True):
                marked = sched.mark_running_jobs_interrupted("gateway shutdown (test)")

        # Both jobs still marked: the closure failure is contained and never
        # escapes shutdown marking.
        assert sorted(marked) == ["job-broken-ledger", "job-healthy"]
