"""A Main turn cannot spawn child work while the Office pause is engaged."""

import json

from agent import estop
from tools.delegate_tool import delegate_task


def test_office_pause_refuses_spawn_before_route_or_model_work(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    estop.office_engage("Main-only test")
    try:
        result = json.loads(delegate_task(goal="new child", parent_agent=object()))
        assert "paused" in result["error"]
    finally:
        estop.office_disengage()


def test_office_pause_preserves_live_child_control(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.delegate_tool._handle_control_action",
                        lambda action, subagent_id, message, parent_agent: action)
    estop.office_engage("Main-only test")
    try:
        assert delegate_task(action="stop", subagent_id="existing", parent_agent=object()) == "stop"
    finally:
        estop.office_disengage()
