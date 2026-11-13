"""Concurrent native task creators must observe the same idempotency key."""
from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    db_path = tmp_path / 'board.db'
    conn = kb.connect(db_path)
    conn.close()
    return db_path


def test_racing_native_creators_return_one_task(board, monkeypatch):
    before_write = threading.Event()
    release = threading.Event()
    original = kb.write_txn
    result, errors = {}, []

    @contextmanager
    def controlled_transaction(conn, **kwargs):
        if threading.current_thread().name == 'racing-native-creator':
            before_write.set()
            if not release.wait(15):
                raise TimeoutError('test did not release native creator')
        with original(conn, **kwargs):
            yield conn

    monkeypatch.setattr(kb, 'write_txn', controlled_transaction)

    def create_first():
        conn = kb.connect(board)
        try:
            result['first'] = kb.create_task(conn, title='first', idempotency_key='same-key',
                                              workspace_kind='scratch', initial_status='blocked')
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=create_first, name='racing-native-creator')
    thread.start()
    try:
        assert before_write.wait(15), 'first creator did not reach the write boundary'
        conn = kb.connect(board)
        try:
            result['second'] = kb.create_task(conn, title='second', idempotency_key='same-key',
                                               workspace_kind='scratch', initial_status='blocked')
        finally:
            conn.close()
    finally:
        release.set()
        thread.join(20)
    assert not thread.is_alive()
    assert not errors
    assert result['first'] == result['second']
    conn = kb.connect(board)
    try:
        rows = conn.execute("SELECT id FROM tasks WHERE idempotency_key = ?", ('same-key',)).fetchall()
        assert [row['id'] for row in rows] == [result['first']]
        count = conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='created'", (result['first'],)).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_archive_empty_keys_and_nested_rollback_keep_existing_behavior(board):
    conn = kb.connect(board)
    try:
        kwargs = {'workspace_kind': 'scratch', 'initial_status': 'blocked'}
        first = kb.create_task(conn, title='first', idempotency_key='key', **kwargs)
        assert kb.create_task(conn, title='repeat', idempotency_key='key', **kwargs) == first
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (first,))
        assert kb.create_task(conn, title='replacement', idempotency_key='key', **kwargs) != first
        assert kb.create_task(conn, title='empty one', idempotency_key='', **kwargs) != kb.create_task(
            conn, title='empty two', idempotency_key='', **kwargs)
        with pytest.raises(RuntimeError, match='synthetic rollback'):
            with kb.write_txn(conn):
                nested = kb.create_task(conn, title='nested', idempotency_key='nested-key', **kwargs)
                assert kb.create_task(conn, title='nested duplicate', idempotency_key='nested-key', **kwargs) == nested
                raise RuntimeError('synthetic rollback')
        assert conn.execute("SELECT count(*) FROM tasks WHERE idempotency_key='nested-key'").fetchone()[0] == 0
    finally:
        conn.close()
