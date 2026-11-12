from hermes_cli import kanban_diagnostics as kd
import pytest


@pytest.mark.parametrize('error', ['newer', None])
def test_mixed_failure_history_does_not_claim_last_runs_all_crashed(error):
    task = {'id': 't_demo', 'status': 'blocked', 'consecutive_failures': 0}
    runs = [
        {'id': 1, 'outcome': 'crashed', 'error': error},
        {'id': 2, 'outcome': 'timed_out'},
        {'id': 3, 'outcome': 'crashed', 'error': error},
    ]
    result = kd.compute_task_diagnostics(task, [], runs, now=100)
    diag = next(d for d in result if d.kind == 'repeated_crashes')
    assert diag.count == 2
    assert 'last 2 runs ended' not in diag.detail
    assert 'since the last completed or reclaimed run' in diag.detail
    runs.append({'id': 4, 'outcome': 'completed'})
    assert not any(d.kind == 'repeated_crashes' for d in kd.compute_task_diagnostics(task, [], runs, now=100))


def test_completed_phantom_warning_does_not_offer_reassignment():
    event = {'kind': 'suspected_hallucinated_references', 'created_at': 10,
             'payload': {'phantom_refs': ['t_deadbeef']}}
    for status in ('done', 'archived'):
        result = kd.compute_task_diagnostics({'id': 't_demo', 'status': status}, [event], [], now=20)
        diag = next(d for d in result if d.kind == 'prose_phantom_refs')
        assert not any(a.kind in ('reassign', 'reclaim') for a in diag.actions)
    result = kd.compute_task_diagnostics({'id': 't_demo', 'status': 'ready'}, [event], [], now=20)
    diag = next(d for d in result if d.kind == 'prose_phantom_refs')
    assert any(a.kind == 'reassign' for a in diag.actions)


def test_cli_diagnostics_reports_synthetic_mixed_history(tmp_path, monkeypatch, capsys):
    import json
    from argparse import Namespace
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban import _cmd_diagnostics

    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    kb.init_db()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title='Synthetic diagnostic')
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        for outcome in ('crashed', 'timed_out', 'crashed'):
            conn.execute("INSERT INTO task_runs (task_id,status,started_at,outcome) VALUES (?,?,?,?)",
                         (task_id, outcome, 1, outcome))
        conn.commit()
    assert _cmd_diagnostics(Namespace(task=task_id, severity=None, json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    crash = next(item for item in rows[0]['diagnostics'] if item['kind'] == 'repeated_crashes')
    assert 'since the last completed or reclaimed run' in crash['detail']
    assert 'last 2 runs ended' not in crash['detail']
