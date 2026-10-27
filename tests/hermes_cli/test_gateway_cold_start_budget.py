"""A large multi-profile cold boot can take longer than a minute."""
from types import SimpleNamespace
import time

import pytest


@pytest.mark.parametrize("explicit_timeout", [None, 4.0])
def test_slow_owner_gets_default_budget_but_explicit_deadline_still_wins(
        tmp_path, monkeypatch, explicit_timeout):
    from hermes_cli import gateway_runtime as runtime
    from hermes_cli.gateway_runtime_service import remaining

    clock = SimpleNamespace(now=0.0)
    endpoint = runtime.GatewayEndpoint(str(tmp_path), "owner", 1, 1,
                                       "http://127.0.0.1:1234", "none", frozenset())

    def observe(home, deadline, replaced):
        remaining(deadline)
        return runtime.GatewayDiscovery("ready", endpoint) if clock.now >= 90 else runtime.GatewayDiscovery("starting")

    monkeypatch.setattr(runtime, "_update_fenced", lambda homes: False)
    monkeypatch.setattr(runtime, "_observe_replacing_stale", observe)
    # Restore the process clock before fixtures tear down; no wall-clock wait or OS spoofing.
    with monkeypatch.context() as timing:
        timing.setattr(time, "monotonic", lambda: clock.now)
        timing.setattr(time, "sleep", lambda seconds: setattr(clock, "now", clock.now + 15))
        kwargs = {} if explicit_timeout is None else {"timeout": explicit_timeout}
        result = runtime.ensure_gateway_runtime(tmp_path, **kwargs)
    if explicit_timeout is None:
        assert result.state == "ready"
        assert result.endpoint == endpoint
    else:
        assert result.state == "starting"
        assert result.reason_code == "deadline"
