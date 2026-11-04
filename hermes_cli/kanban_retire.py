"""Reversible, audited visibility for idle unfinished tasks."""
import time


def list_retired_tasks(conn, *, tenant=None):
    sql = (
        "SELECT t.id, t.title, t.status, r.retired_at, r.reason "
        "FROM task_retirements r JOIN tasks t ON t.id = r.task_id "
    )
    params = ()
    if tenant is not None:
        sql += 'WHERE t.tenant = ? '
        params = (tenant,)
    return [dict(row) for row in conn.execute(sql + 'ORDER BY r.retired_at DESC, t.id', params)]


def retire_task(conn, task_id, reason='', *, actor, expected_status=None):
    from hermes_cli import kanban_db as kb
    if not actor or not actor.strip():
        raise ValueError('actor is required')
    if not reason or not reason.strip():
        raise ValueError('reason is required')
    reason = reason.strip()
    with kb.write_txn(conn):
        row = conn.execute('SELECT status, claim_lock, worker_pid, current_run_id FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('task not found')
        if expected_status is not None and row['status'] != expected_status:
            raise ValueError(f"task status changed: expected {expected_status!r}, found {row['status']!r}")
        if row['status'] in ('done', 'archived', 'running') or any(row[k] is not None for k in ('claim_lock', 'worker_pid', 'current_run_id')):
            raise ValueError('completed or potentially active task cannot be retired')
        if conn.execute("SELECT 1 FROM task_runs WHERE task_id = ? AND status = 'running'", (task_id,)).fetchone():
            raise ValueError('task has an active run')
        changed = conn.execute('INSERT OR IGNORE INTO task_retirements(task_id, retired_at, reason) VALUES (?, ?, ?)', (task_id, int(time.time()), reason)).rowcount == 1
        if changed:
            kb._append_event(conn, task_id, 'retired', {'actor': actor.strip(), 'reason': reason, 'expected_status': expected_status})
        return changed


def restore_retired_task(conn, task_id, *, actor, expected_status, acknowledge_dispatch, tenant=None):
    from hermes_cli import kanban_db as kb
    if not actor or not actor.strip():
        raise ValueError('actor is required')
    if acknowledge_dispatch is not True:
        raise ValueError('dispatch acknowledgment is required')
    with kb.write_txn(conn):
        row = conn.execute('SELECT t.status, t.tenant FROM tasks t JOIN task_retirements r ON r.task_id = t.id WHERE t.id = ?', (task_id,)).fetchone()
        if row is None or (tenant is not None and row['tenant'] != tenant):
            return False
        if row['status'] != expected_status:
            raise ValueError('task status changed; refresh before restoring')
        changed = conn.execute('DELETE FROM task_retirements WHERE task_id = ?', (task_id,)).rowcount == 1
        if changed:
            kb._append_event(conn, task_id, 'retirement_restored', {'actor': actor.strip(), 'expected_status': expected_status, 'acknowledge_dispatch': True})
        return changed


def rollback_retirement_schema(conn, *, writers_quiesced):
    """Remove the additive schema after restoring all cards and stopping writers.

    The caller must coordinate replacing retirement-aware code with the prior
    version before restarting any board process. Existing events are retained.
    """
    from hermes_cli import kanban_db as kb
    if writers_quiesced is not True:
        raise ValueError('all board processes must be stopped before rollback')
    with kb.write_txn(conn):
        if conn.execute('SELECT 1 FROM task_retirements LIMIT 1').fetchone():
            raise ValueError('restore all retired tasks before schema rollback')
        for name in ('retire_freeze_update', 'retire_freeze_delete', 'retire_freeze_link_insert',
                     'retire_freeze_link_update', 'retire_freeze_link_delete'):
            conn.execute('DROP TRIGGER IF EXISTS ' + name)
        conn.execute('DROP TABLE task_retirements')
