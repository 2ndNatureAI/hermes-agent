"""The Office pause holds named-profile ingress and previously queued automatic inputs."""

import asyncio
from types import SimpleNamespace

import pytest

from agent import estop
from gateway.config import Platform
from gateway.run_inbound import GatewayInboundMixin
from gateway.session_authority import SessionAuthority
from hermes_state_runtime import RuntimeStoreError


def _homes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    forge = tmp_path / "profiles" / "forge"
    forge.mkdir(parents=True)
    return tmp_path, forge


def test_office_pause_allows_main_but_refuses_new_named_profile_turn(tmp_path, monkeypatch):
    main, forge = _homes(tmp_path, monkeypatch)
    runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda source: main if source.profile == "default" else forge,
        _hm_estop_turn_allowed=lambda event, source, **kwargs: False,
    )
    event = SimpleNamespace(_heartbeat_session_id=None)
    main_source = SimpleNamespace(profile="default", platform=Platform.LOCAL, chat_id="main")
    forge_source = SimpleNamespace(profile="forge", platform=Platform.DISCORD, chat_id="forge")
    estop.office_engage("Main-only test")
    try:
        assert GatewayInboundMixin._hm_estop_gate(runner, event, main_source, False) is None
        assert "Office work is paused" in GatewayInboundMixin._hm_estop_gate(
            runner, event, forge_source, False)
        assert GatewayInboundMixin._hm_estop_gate(runner, event, forge_source, True) is None
        runner._hm_estop_turn_allowed = lambda event, source, **kwargs: True
        assert GatewayInboundMixin._hm_estop_gate(runner, event, forge_source, False) is None
    finally:
        estop.office_disengage()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,platform", (("forge", Platform.DISCORD), ("default", Platform.WEBHOOK)))
async def test_office_pause_keeps_existing_native_row_queued(tmp_path, monkeypatch, profile, platform):
    main, forge = _homes(tmp_path, monkeypatch)
    import gateway.session_authority as authority_module

    monkeypatch.setattr(authority_module, "list_session_admissions", lambda *args, **kwargs: [
        {"status": "queued", "principal_id": "messaging:existing", "admission_id": "existing"}])
    authority = SimpleNamespace(db=object(), profile_id=str(forge if profile == "forge" else main))
    live = SimpleNamespace(claim_gate=asyncio.Lock(), mutation_lock=asyncio.Lock(),
                           source=SimpleNamespace(platform=platform))
    estop.office_engage("Main-only test")
    try:
        with pytest.raises(RuntimeStoreError, match="office_paused"):
            await SessionAuthority._claim_next(authority, SimpleNamespace(session_id="existing"), live)
    finally:
        estop.office_disengage()
