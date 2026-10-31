"""Lost ACK of a REAL owner commit must reconcile, not poison the managed worker."""
from contextlib import closing
import copy
import json

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution


def test_committed_write_with_lost_answer_recovers_same_receipt_without_duplicate(tmp_path):
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        db.create_session('s', source='cli')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        scope = {'epoch': epoch, 'session_id': 's', 'execution_id': 'worker', 'generation': 0}
        register_worker_execution(db, **scope, kind='compute', adoption_secret='isolated-test')
        requests = []
        def owner(method, **params):
            requests.append(copy.deepcopy(params))
            result = mutate_worker_execution(db, **params)
            if len(requests) == 1:
                raise TimeoutError('worker_receipt_timeout')  # owner committed; only ACK lost
            return result
        store = RuntimeSessionStore(owner, scope, tmp_path / 'outbox')
        try:
            assert store.append_messages_batch('s', [{'role': 'assistant', 'content': 'one-side-effect'}]) == 1
            assert requests[0] == requests[1]
            assert len(requests) == 2
            assert db._read_one("SELECT COUNT(*) FROM messages WHERE content='one-side-effect'")[0] == 1
            assert db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 1
            assert json.loads(store.path.read_text())['pending'] == []
            assert store.failure is None
        finally:
            if store.failure:
                with pytest.raises(WorkerPersistenceError):
                    store.close()
            else:
                store.close()


def test_persistent_uncertainty_stays_journaled_and_fenced_after_one_retry(tmp_path):
    calls = []
    def unavailable(method, **params):
        calls.append(copy.deepcopy(params))
        raise TimeoutError('worker_receipt_timeout')
    store = RuntimeSessionStore(unavailable, {'session_id': 's'}, tmp_path / 'outbox')
    try:
        with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'retained'}])
        assert len(calls) == 2 and calls[0] == calls[1]
        assert len(json.loads(store.path.read_text())['pending']) == 1
        with pytest.raises(WorkerPersistenceError):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'must-not-start'}])
        assert len(calls) == 2
    finally:
        with pytest.raises(WorkerPersistenceError):
            store.close()
