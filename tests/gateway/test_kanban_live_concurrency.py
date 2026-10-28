"""An operator's lower concurrency limit must reach the next embedded tick."""

from types import SimpleNamespace

import pytest

from gateway import kanban_watchers_dispatcher as watcher
from hermes_cli import config_effective as config, kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.config import atomic_config_write, get_config_path


@pytest.fixture
def dispatcher(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with kbc.connect() as conn:
        task = kb.create_task(conn, title="already executing", assignee="default")
        assert kb.claim_task(conn, task) is not None

    def budget(conn, *, board, **kwargs):
        return dispatch._tick_spawn_budget(
            conn, kb.DispatchResult(), board=board,
            max_spawn=kwargs["max_spawn"], max_in_progress=kwargs["max_in_progress"],
        )

    monkeypatch.setattr(watcher, "_kbd", lambda: SimpleNamespace(
        dispatch_once=budget, resolve_max_in_progress=dispatch.resolve_max_in_progress,
    ))
    settings = watcher._DispatcherSettings(60, None, 4, 2, 0, True, None, 2)
    return watcher._KanbanDispatcher(kb, settings)


def test_next_tick_honors_lower_cap_without_recreating_dispatcher(dispatcher):
    current = {"kanban": {"max_in_progress": 4, "max_in_progress_per_profile": 2}}
    atomic_config_write(get_config_path(), current)
    assert dispatcher.tick_once_for_board("default") == (True, 3)

    current["kanban"]["max_in_progress"] = 1
    atomic_config_write(get_config_path(), current)
    assert dispatcher.tick_once_for_board("default") == (False, None)
    with kbc.connect() as conn:
        assert dispatch.count_running_tasks(conn) == 1


def test_unreadable_live_limits_cannot_fall_back_to_boot_headroom(dispatcher, monkeypatch):
    def unreadable(**kwargs):
        raise OSError("config unavailable")

    monkeypatch.setattr(config, "load_user_config_effective", unreadable)
    assert dispatcher.tick_once_for_board("default") is None
    with kbc.connect() as conn:
        assert dispatch.count_running_tasks(conn) == 1
