"""Atomic source-state fence for an operator parking an unclaimed task."""

BLOCKABLE_TASK_SQL = """(status IN ('running', 'ready') OR
    (status = 'todo' AND current_run_id IS NULL AND claim_lock IS NULL))"""


def block_source(conn, task_id, expected_status, kind):
    # Called inside the existing IMMEDIATE write transaction: a TODO cannot
    # become somebody else's running claim between this check and the update.
    row = conn.execute(
        'SELECT status,block_kind,block_recurrences FROM tasks '
        'WHERE id=? AND (? IS NULL OR status=?)',
        (task_id, expected_status, expected_status),
    ).fetchone()
    if row is not None and row['status'] == 'todo' and (expected_status != 'todo' or kind != 'needs_input'):
        return None
    return row
