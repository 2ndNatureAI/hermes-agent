"""Invalid runtime caps must not create immediately expiring tasks."""

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb


@pytest.mark.parametrize("value", [0, -1, 0.5, 1.5, True, float("inf"), 2**63])
def test_invalid_cap_is_rejected_before_task_creation(tmp_path, value):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        with pytest.raises(ValueError, match="max_runtime_seconds"):
            kb.create_task(conn, title="synthetic", max_runtime_seconds=value)
        assert not kb.list_tasks(conn)


@pytest.mark.parametrize("value,expected", [(None, None), (60, 60), ("60", 60)])
def test_valid_caps_are_preserved(tmp_path, value, expected):
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        task_id = kb.create_task(conn, title="synthetic", max_runtime_seconds=value)
        assert kb.get_task(conn, task_id).max_runtime_seconds == expected


@pytest.mark.parametrize("value", ["0", "-1", "0.5s", "1.5s", "nanm", "infh", "1e9999999s"])
def test_cli_rejects_nonpositive_or_fractional_seconds(value):
    with pytest.raises(ValueError):
        cli._parse_duration(value)


@pytest.mark.parametrize("value,expected", [("0.5m", 30), ("1m", 60), ("2h", 7200)])
def test_cli_accepts_exact_whole_second_durations(value, expected):
    assert cli._parse_duration(value) == expected


def test_real_create_tool_does_not_truncate_fractional_cap(tmp_path, monkeypatch):
    from tools import kanban_tools
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    result = json.loads(kanban_tools._handle_create({"title": "synthetic", "assignee": "default",
                                                     "max_runtime_seconds": 1.5}))
    assert result.get("ok") is not True
    assert "max_runtime_seconds" in str(result)


def test_cli_reports_invalid_cap_before_opening_board(monkeypatch, capsys):
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    args = parser.parse_args(["kanban", "create", "synthetic", "--max-runtime=0.5s"])
    monkeypatch.setattr(kb, "connect_closing", lambda **_: pytest.fail("board should not be opened"))
    assert cli._cmd_create(args) == 2
    assert "whole seconds" in capsys.readouterr().err


def test_dashboard_rejects_boolean_cap_before_opening_board(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard import plugin_api
    app = FastAPI()
    app.include_router(plugin_api.router)
    monkeypatch.setattr(plugin_api, "_conn", lambda **_: pytest.fail("board should not be opened"))
    response = TestClient(app).post("/tasks", json={"title": "synthetic", "max_runtime_seconds": True})
    assert response.status_code == 422


def test_dashboard_persists_valid_cap(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard import plugin_api
    app = FastAPI()
    app.include_router(plugin_api.router)
    monkeypatch.setattr(plugin_api, "_resolve_board", lambda board: None)
    monkeypatch.setattr(plugin_api, "_conn", lambda **_: kb.connect(tmp_path / "kanban.db"))
    response = TestClient(app).post("/tasks", json={"title": "synthetic", "max_runtime_seconds": 60})
    assert response.status_code == 200
    task_id = response.json()["task"]["id"]
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        assert kb.get_task(conn, task_id).max_runtime_seconds == 60


def test_swarm_invalid_worker_cap_rolls_back_topology(tmp_path):
    from hermes_cli.kanban_swarm import SwarmWorkerSpec, create_swarm
    worker = SwarmWorkerSpec(profile="worker", title="synthetic", body="work", max_runtime_seconds=0)
    with kb.connect_closing(tmp_path / "kanban.db") as conn:
        with pytest.raises(ValueError, match="max_runtime_seconds"):
            create_swarm(conn, goal="synthetic", workers=[worker],
                         verifier_assignee="reviewer", synthesizer_assignee="writer")
        assert not kb.list_tasks(conn)
