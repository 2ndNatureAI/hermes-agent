"""An offline Windows update must not start a gateway through its atexit fallback."""

from types import SimpleNamespace

import pytest


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("deferred", [False, True])
def test_restart_deferral_does_not_arm_new_windows_cold_starts(monkeypatch, deferred):
    from hermes_cli import update_cmd, update_cmd_windows as windows, update_pause_record
    from hermes_cli._install_repair import _is_windows

    facade = SimpleNamespace(_is_windows=_is_windows,
                             _pause_windows_gateways_for_update=windows._pause_windows_gateways_for_update)
    monkeypatch.setattr(update_cmd, "_m", lambda: facade)

    cold_starts = []

    def cold_start_plan():
        cold_starts.append("installed gateway")
        return {"resume_needed": True, "cold_start_if_installed": True}

    monkeypatch.setattr(windows, "_discover_windows_gateways", lambda: ({}, [], set(), []))
    monkeypatch.setattr(update_pause_record, "adopt_orphans", lambda: (None, []))
    monkeypatch.setattr(update_pause_record, "stamp_tree", lambda token: token)
    monkeypatch.setattr(windows, "_windows_cold_start_plan", cold_start_plan)
    monkeypatch.setattr(windows, "_record_attested_cold_start_profiles", lambda *args: None)

    token = update_cmd._pause_gateways_for_update(SimpleNamespace(no_gateway_restart=deferred))

    if deferred:
        assert token is None
        assert not cold_starts
    else:
        assert token["cold_start_if_installed"] is True
        assert cold_starts == ["installed gateway"]


@pytest.mark.platforms("windows")
def test_deferring_new_cold_starts_keeps_an_adopted_pause_obligation(monkeypatch):
    from hermes_cli import update_cmd_windows as windows, update_pause_record

    adopted = {"profiles": {"default": 12345}}
    recorded = []

    def record_pause(token, orphaned, claims):
        recorded.append((token, orphaned, claims))
        return {**token, "profiles": orphaned["profiles"]}

    def no_new_cold_start(*args):
        pytest.fail("explicit restart deferral must not discover new cold starts")

    monkeypatch.setattr(windows, "_windows_cold_start_plan", no_new_cold_start)
    monkeypatch.setattr(windows, "_record_attested_cold_start_profiles", no_new_cold_start)
    monkeypatch.setattr(update_pause_record, "record_pause", record_pause)

    token = windows._cold_start_pause_token(adopted, ["claim"], allow_cold_start=False)

    assert token["profiles"] == {"default": 12345}
    assert recorded[0][1:] == (adopted, ["claim"])
