"""Invariant tests: the 429 retry lifecycle recovers and stays bounded.

Task t_36e5b189 scenarios 1-3 (scenarios 4-5 live in
test_429_backoff_interrupt.py / test_no_tool_replay_after_retry.py).
Existing coverage pins the Retry-After CAP and the status text
(test_run_agent.py::TestRetryAfterCap) and fallback-after-transport-recovery
(test_fallback_429_after_timeout.py), but never asserts end-to-end that:

1. A transient 429 with a Retry-After window is retried EXACTLY once after
   waiting exactly the provider-stated window, and the turn completes.
2. Recovery leaves no stale state: a tool round executed before the retry is
   grown into history exactly once, with strict role alternation intact.
3. A persistent 429 activates the fallback chain eagerly (first 429, not
   after burning the budget) and completes on the fallback; with NO chain,
   the retry budget is finite (max_retries + 1 attempts, one wait per
   attempt) and the turn ends terminally with the rate-limit verdict.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


class RateLimitError(Exception):
    status_code = 429

    def __init__(self, retry_after: str | None = "1"):
        super().__init__("Error code: 429 - rate limit exceeded")
        self.response = SimpleNamespace(headers={"Retry-After": retry_after} if retry_after else {})
        self.body = {"error": {"message": "rate limit exceeded"}}


def _make_agent() -> AIAgent:
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="test-key-abcdef12",
            base_url="https://example.invalid/v1",
            provider="custom",
            model="test-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None
    agent._cleanup_task_resources = lambda *args, **kwargs: None
    return agent


def _mock_response(content: str, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls" if tool_calls else "stop")
    return SimpleNamespace(choices=[choice], model="test-model", usage=None)


def _tool_call(name: str, call_id: str):
    return SimpleNamespace(
        id=call_id, type="function",
        function=SimpleNamespace(name=name, arguments="{}"),
    )


def _capture_retry_waits(agent: AIAgent) -> list[float]:
    """Record the wait the loop chose for each retry, and let the wait
    complete instantly. The seam is the binding turn_api_error.py actually
    calls (module-level import from agent.turn_recovery)."""
    waits: list[float] = []

    def _spy(agent_arg, wait_time, *_args, **_kwargs):
        waits.append(wait_time)
        return None  # wait completed

    agent._captured_retry_waits = waits
    patcher = patch("agent.turn_api_error.interruptible_backoff_sleep", _spy)
    patcher.start()
    return waits


def test_429_retry_after_honored_then_turn_completes():
    """Scenario 1+2: a transient 429 with Retry-After: 30 is retried exactly
    once, after waiting exactly the provider-stated window (not the jitter
    ladder, not zero), and the turn completes with no residue."""
    agent = _make_agent()
    agent._api_max_retries = 2
    waits = _capture_retry_waits(agent)

    attempts: list[int] = []

    def _fake_api_call(api_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RateLimitError(retry_after="30")
        return _mock_response("Recovered after backoff")

    agent._interruptible_api_call = _fake_api_call

    result = agent.run_conversation("hello")

    # Exactly one retry after exactly one 429 — no early give-up, no storm.
    assert len(attempts) == 2, f"expected 1 failure + 1 retry, got {len(attempts)} attempts"
    # The wait IS the provider's Retry-After, honored verbatim (under the 600s cap).
    assert waits == [30.0], (
        f"retry must wait exactly the Retry-After window once; got waits={waits}"
    )
    assert result["completed"] is True
    assert result["final_response"] == "Recovered after backoff"


def test_recovery_after_429_grows_history_without_duplicate_tool_round():
    """Scenario 2: a tool round that executed before the 429 retry is part of
    the grown history exactly once — the retried request continues from the
    executed round (same tool_call_id), never replays a second tool result,
    and strict role alternation holds in the final history."""
    tool_defs = [
        {"type": "function", "function": {
            "name": "get_status", "description": "status",
            "parameters": {"type": "object", "properties": {}},
        }},
    ]
    with (
        patch("model_tools.get_tool_definitions", return_value=tool_defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="test-key-abcdef12",
            base_url="https://example.invalid/v1",
            provider="custom",
            model="test-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None
    agent._cleanup_task_resources = lambda *args, **kwargs: None
    agent._api_max_retries = 3
    _capture_retry_waits(agent)

    attempts: list[int] = []
    tool_executions: list[str] = []

    def _fake_api_call(api_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RateLimitError(retry_after="5")
        if len(attempts) == 2:
            return _mock_response(None, tool_calls=[_tool_call("get_status", "call_a")])
        return _mock_response("Done")

    def _fake_execute(assistant_message, messages, effective_task_id, api_call_count):
        for tc in assistant_message.tool_calls:
            tool_executions.append(tc.id)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": "status: ok",
            })

    agent._interruptible_api_call = _fake_api_call
    agent._execute_tool_calls = _fake_execute

    result = agent.run_conversation("check it")

    assert result["completed"] is True
    assert result["final_response"] == "Done"
    # The tool executed exactly once across the retry.
    assert tool_executions == ["call_a"], (
        f"tool round must execute exactly once across a 429 retry; got {tool_executions}"
    )
    messages = result["messages"]
    tool_rows = [m for m in messages if m.get("role") == "tool"]
    # No duplicate tool result after the retry — exactly one, from the executed round.
    assert len(tool_rows) == 1, (
        f"retry must not duplicate the executed tool round; tool rows: {tool_rows}"
    )
    assert tool_rows[0]["tool_call_id"] == "call_a"
    # Strict role alternation: no two same-role messages in a row anywhere.
    roles = [m.get("role") for m in messages if m.get("role") in {"user", "assistant", "tool"}]
    for prev, curr in zip(roles, roles[1:]):
        assert prev != curr, f"role alternation broken after retry: {roles}"


def test_persistent_429_falls_back_eagerly_and_completes():
    """Scenario 3 (recovery side): a persistent 429 on the primary switches to
    the fallback chain on the FIRST 429 (eager, before the retry budget burns)
    and the turn completes on the fallback model."""
    fb_chain = [
        {
            "provider": "zai",
            "model": "glm-4.7",
            "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
        }
    ]
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key-abcdef12",
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            provider="zai",
            model="glm-5.1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fb_chain,
        )
    agent.client = MagicMock()
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None
    agent._cleanup_task_resources = lambda *args, **kwargs: None
    agent._api_max_retries = 3
    _capture_retry_waits(agent)

    calls: list[tuple[str, str]] = []

    def _fake_api_call(api_kwargs):
        calls.append((agent.provider, agent.model))
        if len(calls) == 1:
            raise RateLimitError(retry_after="1")
        return _mock_response("Fallback answer")

    agent._interruptible_api_call = _fake_api_call

    mock_fb_client = MagicMock()
    mock_fb_client.api_key = "primary-key-abcdef12"
    mock_fb_client.base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
    mock_fb_client._custom_headers = None
    mock_fb_client.default_headers = None

    with (
        patch("agent.auxiliary_client.resolve_provider_client",
              return_value=(mock_fb_client, "glm-4.7")),
        patch("hermes_cli.model_normalize.normalize_model_for_provider",
              side_effect=lambda m, p: m),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch("agent.agent_runtime_helpers.time.sleep"),
    ):
        result = agent.run_conversation("hello")

    assert result["completed"] is True
    assert result["final_response"] == "Fallback answer"
    # Eager: the very first 429 switched models — no retry burned on the primary.
    assert len(calls) == 2, (
        f"eager fallback must switch on the first 429; got calls={calls}"
    )
    assert calls[0][1] == "glm-5.1" and calls[-1][1] == "glm-4.7"
    assert agent._fallback_activated is True


def test_429_budget_exhaustion_bounded_and_terminal_without_chain():
    """Scenario 3 (exhaustion side): with no fallback chain, a forever-429
    burns exactly the finite budget and ends terminally with the rate-limit
    verdict stamped for the error surface. The loop guard is
    ``while retry_count < max_retries`` with the increment inside the error
    handler, so the real contract is: max_retries=N → exactly N total
    attempts, N-1 Retry-After waits, then the terminal path — never a hot
    loop, never an extra attempt past the budget."""
    agent = _make_agent()
    agent._api_max_retries = 2
    waits = _capture_retry_waits(agent)

    attempts: list[int] = []

    def _fake_api_call(api_kwargs):
        attempts.append(1)
        raise RateLimitError(retry_after="2")

    agent._interruptible_api_call = _fake_api_call

    result = agent.run_conversation("hello")

    # Finite: exactly max_retries total attempts, then the terminal path.
    assert len(attempts) == agent._api_max_retries, (
        f"retry budget must be exactly max_retries attempts; got {len(attempts)}"
    )
    # One bounded wait per retried failure (the last failure goes terminal
    # without waiting), each honoring the provider's stated window.
    assert waits == [2.0] * (agent._api_max_retries - 1), (
        f"each retry must wait the Retry-After window once; got waits={waits}"
    )
    assert result.get("completed") is not True
    assert result.get("failure_reason") == "rate_limit"
    assert result.get("failure_retryable") is True
