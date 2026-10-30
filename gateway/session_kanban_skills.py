"""Required-skill failures are card dependencies, not lost managed executions."""
from pathlib import Path

from hermes_state_runtime import RuntimeStoreError


def unavailable_skills_result(context, missing):
    """Settle normally after parking only this bound attempt; keep review retry provenance."""
    from gateway.session_kanban import _bound_claim_is_live
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    if not _bound_claim_is_live(context):
        raise RuntimeStoreError('stale_kanban_claim')
    names = ', '.join(missing)
    reason = (f'Required Kanban skills unavailable: {names}. '
              f'Owner: profile operator ({context["profile"]}). '
              'Next action: install or enable these skills in the assigned profile, verify preload, '
              'then explicitly resume this card in its original ready/review lane.')
    with connect_closing(Path(context['db'])) as conn:
        if not kb.block_task(conn, context['task_id'], reason=reason, kind='needs_input',
                             expected_run_id=context['run_id']):
            raise RuntimeStoreError('stale_kanban_claim')
    return {'failed': True, 'completed': False, 'failure_reason': 'kanban_skills_unavailable',
            'final_response': reason, 'error': reason}
