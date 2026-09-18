"""Invariant test: a completed tool action is never re-executed by a retry.

Task t_553dd630 scenario 5 (no duplicate tools after retry). The retry loop
wraps ONLY the API call; once a tool round has executed, its result is part
of the conversation history and a subsequent 429 retry re-sends history —
it must never re-run the tool. Red if the loop ever re-executes executed
tool calls on retry.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


class RateLimitError(Exception):
    status_code = 429

    def __init__(self):
        super().__init__("Error code: 429 - rate limit exceeded")
        self.response = SimpleNamespace(headers={"Retry-After": "1"})
        self.body = {"error": {"message": "rate limit exceeded"}}


def _tool_call(name="web_search", call_id="call_test123"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments="{}"),
    )


def _response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


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
    agent._persist_session = lambda *args, **kwargs: None
    agent._save_trajectory = lambda *args, **kwargs: None
    # Register the fake tool so the tool round treats the call as valid and
    # actually dispatches execution (empty valid_tool_names = error-result
    # without execution).
    agent.tools = [{"type": "function",
                    "function": {"name": "web_search", "description": "fake",
                                 "parameters": {"type": "object", "properties": {}}}}]
    agent.valid_tool_names = {"web_search"}
    return agent


def test_tool_executes_once_across_429_retry(monkeypatch):
    """Sequence: API -> tool call -> tool executes -> next API call 429s ->
    retry succeeds with final text. The tool must have executed EXACTLY ONCE;
    the retry resends history, it does not replay actions."""
    agent = _make_agent()
    tool_executions: list[str] = []

    responses = iter([
        _response(content="", finish_reason="tool_calls", tool_calls=[_tool_call()]),
        RateLimitError(),
        _response(content="All done", finish_reason="stop"),
    ])

    def _fake_api_call(api_kwargs):
        r = next(responses)
        if isinstance(r, Exception):
            raise r
        return r

    agent._interruptible_api_call = _fake_api_call

    def _fake_execute_tool_calls(assistant_message, messages, task_id, api_call_count):
        tool_executions.append(getattr(assistant_message, "id", "") or "msg")
        for message in messages:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                message.setdefault("extra_content", {})
        messages.append({"role": "tool", "tool_call_id": "call_test123",
                         "content": "tool result"})
        return "tool result"

    monkeypatch.setattr(agent, "_execute_tool_calls", _fake_execute_tool_calls)
    monkeypatch.setattr(
        "agent.turn_api_error.interruptible_backoff_sleep", lambda *a, **k: None
    )

    result = agent.run_conversation("search for it")

    assert result.get("completed") is True, "turn should complete after the retry succeeds"
    assert len(tool_executions) == 1, (
        f"tool executed {len(tool_executions)} times across a 429 retry; "
        "completed tool actions must never be replayed"
    )


def test_tool_result_survives_retry_history(monkeypatch):
    """The retried API request must carry the executed tool round as history
    (assistant tool_calls + tool result), not a bare repeat of the user turn —
    a resubmit that dropped the tool result would be an implicit replay of the
    action from the model's perspective."""
    agent = _make_agent()

    responses = iter([
        _response(content="", finish_reason="tool_calls", tool_calls=[_tool_call()]),
        RateLimitError(),
        _response(content="All done", finish_reason="stop"),
    ])

    seen_request_bodies: list[list[str]] = []

    def _fake_api_call(api_kwargs):
        r = next(responses)
        if isinstance(r, Exception):
            raise r
        body = (api_kwargs or {}).get("messages") or []
        seen_request_bodies.append([str(m.get("role")) for m in body if isinstance(m, dict)])
        return r

    agent._interruptible_api_call = _fake_api_call

    def _fake_execute_tool_calls(assistant_message, messages, task_id, api_call_count):
        messages.append({"role": "tool", "tool_call_id": "call_test123",
                         "content": "tool result"})
        return "tool result"

    monkeypatch.setattr(agent, "_execute_tool_calls", _fake_execute_tool_calls)
    monkeypatch.setattr(
        "agent.turn_api_error.interruptible_backoff_sleep", lambda *a, **k: None
    )

    agent.run_conversation("search for it")

    # Attempt 1 carries the user turn (plus system/preflight rows). The retry
    # must carry STRICTLY MORE (the executed tool round appended) and must
    # include the tool result message. If the retry dropped the tool round,
    # the role sequence would be identical to attempt 1.
    assert len(seen_request_bodies) == 2
    assert len(seen_request_bodies[1]) > len(seen_request_bodies[0]), (
        f"retry request did not grow after the tool round: "
        f"attempt1={len(seen_request_bodies[0])} attempt2={len(seen_request_bodies[1])}"
    )
    assert seen_request_bodies[1].count("tool") >= 1, (
        "retry request must include the executed tool result as history; "
        f"roles seen: {seen_request_bodies[1]}"
    )
