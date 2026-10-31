"""An owned resource hold must survive dependency completion without stealing a raced claim."""
from contextlib import closing

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


def test_guarded_todo_hold_survives_all_parents_completing(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    kb.init_db()
    with closing(connect()) as conn:
        parent = kb.create_task(conn, title='prerequisite', assignee='default')
        child = kb.create_task(conn, title='resource-held experiment', assignee='default', initial_status='blocked')
        kb.link_tasks(conn, parent, child)
        assert kb.unblock_task(conn, child)
        assert kb.get_task(conn, child).status == 'todo'
        assert not kb.block_task(conn, child, reason='legacy TODO call remains refused')
        assert not kb.block_task(conn, child, expected_status='todo', kind='dependency')
        assert kb.block_task(conn, child, expected_status='todo', kind='needs_input', reason='owned capacity recheck')
        claimed = kb.claim_task(conn, parent, claimer='isolated-prerequisite-owner')
        assert claimed is not None
        assert kb.complete_task(conn, parent, expected_run_id=claimed.current_run_id, result='fixture prerequisite satisfied')
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == 'blocked'
        assert kb.get_task(conn, child).claim_lock is None


def test_guarded_todo_hold_refuses_a_new_running_owner(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    kb.init_db()
    with closing(connect()) as conn:
        child = kb.create_task(conn, title='newly claimed', assignee='default')
        claimed = kb.claim_task(conn, child, claimer='actual-owner')
        assert claimed is not None
        before = kb.get_task(conn, child)
        assert not kb.block_task(conn, child, expected_status='todo', kind='needs_input', reason='stale observer')
        after = kb.get_task(conn, child)
        assert (after.status, after.current_run_id, after.claim_lock) == (before.status, before.current_run_id, before.claim_lock)
