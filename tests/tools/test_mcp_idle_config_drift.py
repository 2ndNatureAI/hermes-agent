"""A live stdio MCP idle limit follows its owner's config without tearing down calls."""

import asyncio
import time
from unittest.mock import patch

from tools.mcp_tool import MCPServerTask


def test_idle_limit_change_wakes_existing_stdio_session():
    async def exercise():
        server = MCPServerTask("fetch")
        server._config = {"command": "uvx"}
        server._session_proven = True
        server._last_tool_call_at = time.monotonic() - 1000
        waiter = asyncio.create_task(server._wait_for_lifecycle_event())
        await asyncio.sleep(0)

        assert server.update_idle_timeout(900) is True
        assert await asyncio.wait_for(waiter, timeout=2) == "recycle"
        assert server._recycled_reason == "idle_timeout_seconds"
        assert not server._reconnect_event.is_set()

    asyncio.run(exercise())


def test_idle_limit_change_waits_for_active_rpc():
    async def exercise():
        server = MCPServerTask("fetch")
        server._config = {"command": "uvx"}
        server._session_proven = True
        server._last_tool_call_at = time.monotonic() - 1000
        with patch.object(MCPServerTask, "_fail_inflight_calls", side_effect=AssertionError("call interrupted")):
            async with server._rpc_lock:
                waiter = asyncio.create_task(server._wait_for_lifecycle_event())
                await asyncio.sleep(0)
                assert server.update_idle_timeout(900) is True
                await asyncio.sleep(0)
                assert not waiter.done(), "an active RPC must not be recycled or failed"
            assert await asyncio.wait_for(waiter, timeout=2) == "recycle"

    asyncio.run(exercise())


def test_reconcile_updates_only_unshared_owner_connection(monkeypatch, tmp_path):
    from tools import mcp_tool as core
    from tools import mcp_tool_config as config
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool_lifecycle as lifecycle
    from tools import mcp_tool_loop as loop

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner = "A"
    definitions = {"fetch": {"command": "uvx", "idle_timeout_seconds": 900},
                   "shared": {"command": "uvx", "idle_timeout_seconds": 300}}
    own = MCPServerTask("fetch")
    own._config = {"command": "uvx"}
    adopted = MCPServerTask("shared")
    adopted._config = {"command": "uvx"}
    monkeypatch.setattr(core, "_mcp_registry_scope", lambda: owner)
    monkeypatch.setattr(config, "_load_mcp_config", lambda: definitions)
    monkeypatch.setattr(discovery, "discover_mcp_tools", lambda: (_ for _ in ()).throw(AssertionError("rediscovery")))
    monkeypatch.setattr(lifecycle, "shutdown_mcp_servers", lambda **kw: (_ for _ in ()).throw(AssertionError("shutdown")))
    scheduled = []

    def run_on_loop(factory, timeout):
        scheduled.append(timeout)
        return asyncio.run(factory())

    monkeypatch.setattr(loop, "_run_on_mcp_loop", run_on_loop)
    with core._lock:
        core._servers[("A", "fetch")] = own
        core._server_scope_keys[("A", "fetch")] = "A"
        core._server_tool_scopes[("A", "fetch")] = {"A"}
        core._servers[("B", "shared")] = adopted
        core._server_scope_keys[("B", "shared")] = "B"
        core._server_tool_scopes[("B", "shared")] = {"A", "B"}
    try:
        empty = {"removed": [], "added": [], "pending": []}
        assert discovery.reconcile_mcp_servers_with_config() == empty
        assert own._idle_timeout_seconds == 900
        assert adopted._idle_timeout_seconds is None
        assert discovery.reconcile_mcp_servers_with_config() == empty
        assert scheduled == [5], "unchanged config must not schedule another update"

        owner = "B"
        definitions = {"shared": {"command": "uvx", "idle_timeout_seconds": 300}}
        assert discovery.reconcile_mcp_servers_with_config() == empty
        assert adopted._idle_timeout_seconds is None, "an adopter must not change its owner's task"
        assert scheduled == [5]
    finally:
        with core._lock:
            for key in (("A", "fetch"), ("B", "shared")):
                core._servers.pop(key, None)
                core._server_scope_keys.pop(key, None)
                core._server_tool_scopes.pop(key, None)
