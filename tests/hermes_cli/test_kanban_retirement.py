import sqlite3
import contextlib
import importlib.util
import sys
import json
import argparse
from pathlib import Path
import pytest
from fastapi import FastAPI
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db_connect import connect

@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / 'isolated.db')
    yield conn
    conn.close()

def task(db, name, status='ready', workspace=None):
    db.execute('INSERT INTO tasks (id,title,status,created_at,workspace_kind,workspace_path) VALUES (?,?,?,?,?,?)', (name,name,status,1,'scratch',workspace))
    return name

def test_visibility_dependency_dispatch_and_restore(db, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'kept.txt').write_text('important')
    task(db, 'parent', workspace=str(workspace))
    task(db, 'child', 'todo')
    task(db, 'completed', 'done')
    db.execute('INSERT INTO task_links(parent_id,child_id) VALUES (?,?)', ('parent','child'))
    assert kb.retire_task(db, 'parent', 'replaced', actor="test")
    assert {x.id for x in kb.list_tasks(db)} == {'child', 'completed'}
    assert {x.id for x in kb.list_tasks(db, include_retired=True)} == {'parent','child','completed'}
    assert dispatch._lane_rows(db, 'ready') == []
    assert kb.claim_task(db, 'parent') is None
    with kb.write_txn(db):
        assert kb._claim_and_open_run(db, 'parent', 'ready', 'attempt', 100, 1) is None
    kb.recompute_ready(db)
    assert kb.get_task(db, 'child').status == 'todo'
    assert kb.get_task(db, 'parent').status == 'ready'
    assert (workspace / 'kept.txt').read_text() == 'important'
    assert db.execute('SELECT COUNT(*) FROM task_links').fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM task_links WHERE parent_id='parent'")
    assert kb.restore_retired_task(db, 'parent', actor='test', expected_status='ready', acknowledge_dispatch=True)
    assert {x.id for x in kb.list_tasks(db)} == {'parent','child','completed'}
    assert [x['id'] for x in dispatch._lane_rows(db, 'ready')] == ['parent']
    assert kb.get_task(db, 'parent').workspace_path == str(workspace)

def test_idle_only_and_review_guard(db):
    for name, status in [('running','running'), ('done','done'), ('archived','archived')]:
        task(db, name, status)
        with pytest.raises(ValueError):
            kb.retire_task(db, name, reason="test", actor="test")
    task(db, 'review', 'review')
    kb.retire_task(db, 'review', reason="test", actor="test")
    assert kb.claim_review_task(db, 'review') is None
    assert kb.get_task(db, 'review').status == 'review'
    task(db, 'orphan')
    db.execute("UPDATE tasks SET worker_pid=7 WHERE id='orphan'")
    with pytest.raises(ValueError):
        kb.retire_task(db, 'orphan', reason="test", actor="test")

def test_retired_todo_not_promoted(db):
    task(db, 'todo', 'todo')
    kb.retire_task(db, 'todo', reason="test", actor="test")
    kb.recompute_ready(db)
    assert kb.get_task(db, 'todo').status == 'todo'
    with pytest.raises(sqlite3.IntegrityError):
        kb.promote_task(db, 'todo', actor='test')
    with pytest.raises(sqlite3.IntegrityError):
        kb.edit_task(db, 'todo', title='mutated')
    with pytest.raises(sqlite3.IntegrityError):
        kb.delete_task(db, 'todo')
    assert kb.get_task(db, 'todo').title == 'todo'
    kb.restore_retired_task(db, 'todo', actor='test', expected_status='todo', acknowledge_dispatch=True)
    assert kb.promote_task(db, 'todo', actor='test')[0]

def test_real_board_response_hides_retired(db, monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'plugins' / 'kanban' / 'dashboard' / 'plugin_api.py'
    spec = importlib.util.spec_from_file_location('retire_test_plugin_api', path)
    api = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, api)
    spec.loader.exec_module(api)
    @contextlib.contextmanager
    def board_conn(board):
        yield 'isolated', db
    monkeypatch.setattr(api, '_board_conn', board_conn)
    monkeypatch.setattr(kb, 'known_assignees', lambda conn: [])
    task(db, 'hide')
    task(db, 'done', 'done')
    kb.retire_task(db, 'hide', reason="test", actor="test")
    result = api.get_board(tenant=None, include_archived=False, board='isolated', workflow_template_id=None, current_step_key=None)
    ids = {item['id'] for col in result['columns'] for item in col['tasks']}
    assert ids == {'done'}
    assert api.get_stats(board='isolated')['by_status'] == {'done': 1}
    assert api.list_retirements(board='isolated', tenant=None)['tasks'][0]['id'] == 'hide'
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as refused:
        api.update_task('hide', None, board='isolated')
    assert refused.value.status_code == 409
    with pytest.raises(HTTPException) as refused:
        api.delete_task('hide', board='isolated')
    assert refused.value.status_code == 409
    batch = api.bulk_update(api.BulkTaskBody(ids=['hide'], status='todo'), board='isolated')
    assert batch['results'] == [{'id': 'hide', 'ok': False, 'error': 'Restore the retired task before changing it'}]
    path = Path(db.execute('PRAGMA database_list').fetchone()['file'])
    monkeypatch.setattr(kb, 'kanban_db_path', lambda **kwargs: path)
    from hermes_cli import kanban_db_connect as connector
    def open_board(**kwargs):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(connector, 'connect', open_board)
    assert api._board_counts('isolated') == {'done': 1}
    assert api.restore_retirement('hide', api.RestoreRetirementBody(expected_status='ready', acknowledge_dispatch=True), board='isolated')['restored']
    events = kb.list_events(db, 'hide')
    assert [e.kind for e in events] == ['retired', 'retirement_restored']
    assert events[-1].payload['actor'] == 'dashboard'
    assert api.list_retirements(board='isolated', tenant=None)['tasks'] == []
    result = api.get_board(tenant=None, include_archived=False, board='isolated', workflow_template_id=None, current_step_key=None)
    ids = {item['id'] for col in result['columns'] for item in col['tasks']}
    assert ids == {'hide', 'done'}
    assert api.get_stats(board='isolated')['by_status'] == {'done': 1, 'ready': 1}
    assert api._board_counts('isolated') == {'done': 1, 'ready': 1}

def test_board_connection_maps_retirement_trigger_only(db, monkeypatch):
    from fastapi import HTTPException
    path = Path(__file__).resolve().parents[2] / 'plugins' / 'kanban' / 'dashboard' / 'plugin_api.py'
    spec = importlib.util.spec_from_file_location('retire_mapping_api', path)
    api = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, api)
    spec.loader.exec_module(api)
    path = Path(db.execute('PRAGMA database_list').fetchone()['file'])
    monkeypatch.setattr(api, '_resolve_board', lambda value: value)
    def open_board(**kwargs):
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(api, '_conn', open_board)
    task(db, 'frozen')
    task(db, 'linked', 'todo')
    task(db, 'newchild', 'todo')
    db.execute("INSERT INTO task_links(parent_id, child_id) VALUES ('frozen', 'linked')")
    kb.retire_task(db, 'frozen', reason="test", actor='test')
    with pytest.raises(HTTPException) as refused:
        with api._board_conn('isolated') as (_, conn):
            conn.execute("UPDATE tasks SET title='changed' WHERE id='frozen'")
    assert refused.value.status_code == 409
    with pytest.raises(HTTPException) as refused:
        api.add_link(api.LinkBody(parent_id='frozen', child_id='newchild'), board='isolated')
    assert refused.value.status_code == 409
    with pytest.raises(HTTPException) as refused:
        api.delete_link(parent_id='frozen', child_id='linked', board='isolated')
    assert refused.value.status_code == 409
    assert db.execute("SELECT COUNT(*) FROM task_links WHERE parent_id='frozen'").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        with api._board_conn('isolated') as (_, conn):
            conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('frozen','duplicate','ready',1)")

def test_retire_requires_reason_and_actor(db):
    task(db, 'unfinished')
    with pytest.raises(ValueError, match='reason'):
        kb.retire_task(db, 'unfinished', actor='test')
    with pytest.raises(ValueError, match='actor'):
        kb.retire_task(db, 'unfinished', reason='cleanup', actor=' ')
    assert kb.list_retired_tasks(db) == []

def test_operator_cli_one_card_checks_status_and_records_event(db, monkeypatch, capsys):
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_parser
    parser = argparse.ArgumentParser()
    kanban_parser.build_parser(parser.add_subparsers(dest='command'))
    with pytest.raises(SystemExit):
        parser.parse_args(['kanban', '--board', 'default', 'retire', 'one', '--reason', 'reason'])
    with pytest.raises(SystemExit):
        parser.parse_args(['kanban', '--board', 'default', 'retire', 'one', 'two', '--expected-status', 'ready', '--reason', 'reason'])
    task(db, 'one', 'ready')
    task(db, 'finished', 'done')
    @contextlib.contextmanager
    def connection():
        yield db
    monkeypatch.setattr(cli.kbc, 'connect_closing', connection)
    monkeypatch.setattr(cli.kb, 'init_db', lambda: None)
    monkeypatch.setattr(cli.kb, 'scoped_current_board', lambda board: contextlib.nullcontext())
    monkeypatch.setattr(cli, '_profile_author', lambda: 'operator-profile')
    monkeypatch.setattr(cli, '_is_delegated_child_cli_mutation', lambda args: False)
    missing = parser.parse_args(['kanban', 'retire', 'one', '--expected-status', 'ready', '--reason', 'cleanup'])
    assert cli.kanban_command(missing) == 2
    stale = parser.parse_args(['kanban', '--board', 'default', 'retire', 'one', '--expected-status', 'todo', '--reason', 'cleanup'])
    assert cli.kanban_command(stale) != 0
    assert kb.list_retired_tasks(db) == []
    assert kb.list_events(db, 'one') == []
    db.execute("UPDATE tasks SET claim_lock='owner' WHERE id='one'")
    current = parser.parse_args(['kanban', '--board', 'default', 'retire', 'one', '--expected-status', 'ready', '--reason', 'cleanup'])
    assert cli.kanban_command(current) != 0
    assert kb.list_events(db, 'one') == []
    db.execute("UPDATE tasks SET claim_lock=NULL WHERE id='one'")
    assert cli.kanban_command(current) == 0
    out = capsys.readouterr().out
    assert 'Retired one' in out and 'event recorded' in out
    event = kb.list_events(db, 'one')[0]
    assert event.kind == 'retired'
    assert event.payload == {'actor':'operator-profile', 'reason':'cleanup', 'expected_status':'ready'}
    assert kb.get_task(db, 'finished').status == 'done'
    assert 'retire' in cli._DELEGATED_CHILD_DENIED_ACTIONS

def test_routes_through_asgi_without_event_loop(db, monkeypatch):
    import fastapi.routing
    import starlette.routing
    path = Path(__file__).resolve().parents[2] / 'plugins' / 'kanban' / 'dashboard' / 'plugin_api.py'
    spec = importlib.util.spec_from_file_location('retire_asgi_api', path)
    api = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, api)
    spec.loader.exec_module(api)
    @contextlib.contextmanager
    def board_conn(board):
        yield 'isolated', db
    monkeypatch.setattr(api, '_board_conn', board_conn)
    async def inline_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr(starlette.routing, 'run_in_threadpool', inline_threadpool)
    monkeypatch.setattr(fastapi.routing, 'run_in_threadpool', inline_threadpool)
    app = FastAPI()
    app.include_router(api.router)
    task(db, 'transport')
    task(db, 'beta')
    task(db, 'finished', 'done')
    db.execute("UPDATE tasks SET tenant='alpha' WHERE id='transport'")
    db.execute("UPDATE tasks SET tenant='beta' WHERE id='beta'")
    kb.retire_task(db, 'transport', reason='replacement', actor='test')
    kb.retire_task(db, 'beta', reason='replacement', actor='test')
    def request(method, path, payload=None, query=b'board=isolated'):
        body = json.dumps(payload).encode() if payload is not None else b''
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                 'query_string': query, 'root_path': '', 'server': ('test', 80),
                 'client': ('test', 1), 'headers': [(b'content-type', b'application/json')]}
        sent = []
        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}
        async def send(message):
            sent.append(message)
        coro = app(scope, receive, send)
        try:
            yielded = coro.send(None)
        except StopIteration:
            pass
        else:
            coro.close()
            raise AssertionError(f'ASGI request unexpectedly required event loop: {yielded!r}')
        return sent[0]['status'], json.loads(b''.join(m.get('body', b'') for m in sent[1:]))
    status, result = request('GET', '/retirements', query=b'board=isolated&tenant=alpha')
    assert status == 200 and result['tasks'][0]['id'] == 'transport'
    assert [t['id'] for t in result['tasks']] == ['transport']
    assert [t['id'] for t in request('GET', '/retirements', query=b'board=isolated&tenant=beta')[1]['tasks']] == ['beta']
    assert request('PATCH', '/tasks/transport', {'title':'changed'})[0] == 409
    assert request('DELETE', '/tasks/transport')[0] == 409
    assert request('POST', '/retirements/transport/restore', {'expected_status':'ready'})[0] == 409
    assert request('POST', '/retirements/transport/restore', {'expected_status':'todo','acknowledge_dispatch':True})[0] == 409
    assert request('POST', '/retirements/transport/restore', {'expected_status':'ready','acknowledge_dispatch':True,'tenant':'beta'})[0] == 404
    assert kb.claim_task(db, 'transport') is None
    assert request('POST', '/retirements/transport/restore', {'expected_status':'ready','acknowledge_dispatch':True,'tenant':'alpha'})[0] == 200
    assert request('GET', '/retirements', query=b'board=isolated&tenant=alpha')[1]['tasks'] == []
    assert kb.get_task(db, 'finished').status == 'done'

def test_stats_exclude_retired_and_restore_counts_and_age(db, monkeypatch):
    task(db, 'older')
    task(db, 'newer')
    task(db, 'finished', 'done')
    task(db, 'archived', 'archived')
    db.execute("UPDATE tasks SET assignee='person', created_at=100 WHERE id='older'")
    db.execute("UPDATE tasks SET assignee='person', created_at=200 WHERE id='newer'")
    db.execute("UPDATE tasks SET assignee='person' WHERE id='finished'")
    db.execute("UPDATE tasks SET assignee='person' WHERE id='archived'")
    monkeypatch.setattr(kb.time, 'time', lambda: 300)
    kb.retire_task(db, 'older', reason="test", actor="test")
    stats = kb.board_stats(db)
    assert stats['by_status'] == {'done': 1, 'ready': 1}
    assert stats['by_assignee'] == {'person': {'done': 1, 'ready': 1}}
    assert stats['oldest_ready_age_seconds'] == 100
    kb.retire_task(db, 'newer', reason="test", actor="test")
    stats = kb.board_stats(db)
    assert stats['by_status'] == {'done': 1}
    assert stats['by_assignee'] == {'person': {'done': 1}}
    assert stats['oldest_ready_age_seconds'] is None
    kb.restore_retired_task(db, 'older', actor='test', expected_status='ready', acknowledge_dispatch=True)
    stats = kb.board_stats(db)
    assert stats['by_status'] == {'done': 1, 'ready': 1}
    assert stats['oldest_ready_age_seconds'] == 200

def test_existing_database_receives_schema(tmp_path):
    from hermes_cli import kanban_db_connect as connector
    path = tmp_path / 'existing.db'
    conn = connect(path)
    conn.executescript('''
    DROP TRIGGER retire_freeze_update;
    DROP TRIGGER retire_freeze_delete;
    DROP TRIGGER retire_freeze_link_insert;
    DROP TRIGGER retire_freeze_link_update;
    DROP TRIGGER retire_freeze_link_delete;
    DROP TABLE task_retirements;
    ''')
    conn.close()
    connector._INITIALIZED_PATHS.discard(str(path.resolve()))
    conn = connect(path)
    try:
        task(conn, 'old')
        assert kb.retire_task(conn, 'old', reason="test", actor='test')
        assert kb.list_tasks(conn) == []
    finally:
        conn.close()

def test_schema_rollback_after_restoring_all_cards(db):
    from hermes_cli.kanban_retire import rollback_retirement_schema
    task(db, 'preserved', 'todo')
    task(db, 'complete', 'done')
    kb.retire_task(db, 'preserved', reason="test", actor="test")
    with pytest.raises(ValueError, match='stopped'):
        rollback_retirement_schema(db, writers_quiesced=False)
    with pytest.raises(ValueError, match='restore all'):
        rollback_retirement_schema(db, writers_quiesced=True)
    assert len(kb.list_retired_tasks(db)) == 1
    assert kb.restore_retired_task(db, 'preserved', actor='test', expected_status='todo', acknowledge_dispatch=True)
    rollback_retirement_schema(db, writers_quiesced=True)
    db.execute("UPDATE tasks SET status='ready' WHERE id='preserved'")
    assert db.execute("SELECT status FROM tasks WHERE id='preserved'").fetchone()[0] == 'ready'
    assert db.execute("SELECT status FROM tasks WHERE id='complete'").fetchone()[0] == 'done'
    assert db.execute("SELECT count(*) FROM task_events WHERE task_id='preserved'").fetchone()[0] == 2
