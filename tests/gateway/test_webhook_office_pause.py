"""Authenticated webhook ingress stays retryable while automatic Office work is paused."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import estop
from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("agent", "coalesced", "deliver_only", "cron_job"))
async def test_office_pause_refuses_all_webhook_modes_before_ack(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    route = {"secret": _INSECURE_NO_AUTH, "prompt": "event"}
    if mode == "coalesced":
        route["coalesce"] = {"key": "id", "window_seconds": 1}
    elif mode == "deliver_only":
        route.update(deliver_only=True, deliver="telegram")
    elif mode == "cron_job":
        route["cron_job"] = "existing-job"
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0, "routes": {"office": route}}))
    calls = []

    async def dispatch(*args, **kwargs):
        calls.append("agent")
        return web.Response(status=202)

    adapter._dispatch_agent_run = dispatch
    adapter._handle_deliver_only = dispatch
    adapter._handle_cron_trigger = dispatch
    adapter._coalescer.enqueue = lambda **kwargs: calls.append("coalesce") or True
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    estop.office_engage("hold automatic work")
    try:
        async with TestClient(TestServer(app)) as client:
            headers = {"X-GitHub-Delivery": "retryable-delivery"}
            response = await client.post("/webhooks/office", json={"id": 7}, headers=headers)
            assert response.status == 503
            assert (await response.json())["status"] == "paused"
            assert calls == []
            estop.office_disengage()
            retry = await client.post("/webhooks/office", json={"id": 7}, headers=headers)
            assert retry.status == 202
            assert calls
    finally:
        estop.office_disengage()


@pytest.mark.asyncio
async def test_buffered_webhook_cannot_admit_after_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0, "routes": {"office": {"secret": _INSECURE_NO_AUTH}}}))
    adapter.build_source = lambda **kwargs: pytest.fail("admission proceeded while paused")
    estop.office_engage("hold automatic work")
    try:
        outcome = await adapter._admit_agent_run(
            {"id": 7}, "event", "delivery", 0.0, route_config={}, route_name="office",
            profile=None, event_type="test")
        assert outcome is None
    finally:
        estop.office_disengage()
