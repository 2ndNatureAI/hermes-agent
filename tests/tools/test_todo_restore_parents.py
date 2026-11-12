import json

import pytest

from run_agent import AIAgent
from tools.todo_tool import MAX_TODO_ITEMS, TodoStore


@pytest.mark.parametrize('todos', [
    [{'id': 'a', 'content': 'Keep this task', 'status': 'pending', 'parent': 'missing'}],
    [{'id': 'a', 'content': 'First task', 'status': 'pending', 'parent': 'b'},
     {'id': 'b', 'content': 'Second task', 'status': 'in_progress', 'parent': 'a'}],
])
def test_resumed_history_keeps_active_tasks_visible_after_compression(todos, monkeypatch):
    agent = AIAgent.__new__(AIAgent)
    agent._todo_store = TodoStore()
    agent.quiet_mode = True
    monkeypatch.setattr('run_agent._set_interrupt', lambda *_: None)
    history = [
        {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'type': 'function',
            'function': {'name': 'todo', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': json.dumps({'todos': todos, 'revision': 7})},
    ]
    agent._hydrate_todo_store(history)
    assert agent._todo_store.snapshot()['revision'] == 7
    injected = agent._todo_store.format_for_injection()
    assert injected is not None
    for todo in todos:
        assert todo['content'] in injected


def test_restore_keeps_valid_parent_and_revision():
    store = TodoStore()
    todos = [{'id': 'p', 'content': 'Parent', 'status': 'completed'},
             {'id': 'c', 'content': 'Child', 'status': 'pending', 'parent': 'p'}]
    store.restore(todos, revision=5)
    assert store.snapshot() == {'todos': todos, 'revision': 5}
    assert 'Parent' in store.format_for_injection()
    assert 'Child' in store.format_for_injection()


def test_restore_repairs_parent_removed_by_item_limit():
    store = TodoStore()
    todos = [{'id': 'child', 'content': 'Visible child', 'status': 'pending', 'parent': 'parent'}]
    todos.extend({'id': str(i), 'content': 'Finished', 'status': 'completed'}
                 for i in range(MAX_TODO_ITEMS - 1))
    todos.append({'id': 'parent', 'content': 'Parent', 'status': 'completed'})
    store.restore(todos, revision=4)
    assert store.read()[0].get('parent') is None
    assert 'Visible child' in store.format_for_injection()
    assert store.snapshot()['revision'] == 4
