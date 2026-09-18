"""Invariant tests: interrupt during the 429 retry/backoff path must stop the
retry loop promptly, with no zombie retries after Stop.

Task t_553dd630 scenario 4 (stop/cancel during retry wait cancels promptly,
no zombie turn). Existing coverage interrupts during backoff to keep tests
fast (test_run_agent.py::_drive_once) but never asserts that the interrupt
STOPS the retries — the loop only got a fast path, not a verdict.

Two behaviors, red if broken:
1. An interrupt raised during the Retry-After wait for a 429 ends the turn
   (no further API attempts, no completion).
2. An interrupt raised before the FIRST attempt of a turn with a 429-ing
   provider never reaches the API at all (no zombie attempt after Stop).
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
    return agent


def _interrupt_after_marker(agent: AIAgent, marker: str) -> None:
    """Set _interrupt_requested the first time the loop reports waiting on the
    Retry-After window, the same seam test_run_agent.py uses."""
    original_buffer = agent._buffer_status
    original_emit = agent._emit_status

    def _capture_buffer(msg, *args, **kwargs):
        if marker in msg:
            agent._interrupt_requested = True
        return original_buffer(msg, *args, **kwargs)

    def _capture_emit(msg):
        if marker in msg:
            agent._interrupt_requested = True
        return original_emit(msg)

    agent._buffer_status = _capture_buffer
    agent._emit_status = _capture_emit


def _interrupt_on_diagnostic_wait(agent: AIAgent) -> None:
    """Set _interrupt_requested when the loop reports the retry wait — the
    diagnostic status seam the 429 backoff path actually uses
    (_emit_diagnostic_wait fires for every retry wait; the buffered/emit
    status variants carry the same text)."""
    original_wait = agent._emit_diagnostic_wait
    original_buffer = agent._buffer_diagnostic_status
    original_emit = agent._emit_diagnostic_status
    marker = "waiting on provider"

    def _capture_wait(text):
        if marker in text:
            agent._interrupt_requested = True
        return original_wait(text)

    def _capture_buffer(msg, *args, **kwargs):
        if marker in msg:
            agent._interrupt_requested = True
        return original_buffer(msg, *args, **kwargs)

    def _capture_emit(msg):
        if marker in msg:
            agent._interrupt_requested = True
        return original_emit(msg)

    agent._emit_diagnostic_wait = _capture_wait
    agent._buffer_diagnostic_status = _capture_buffer
    agent._emit_diagnostic_status = _capture_emit


def test_429_backoff_interrupt_stops_retries():
    """Interrupt during the 429 Retry-After wait ends the turn: no further API
    attempts, turn does not complete. The REAL interruptible_backoff_sleep
    runs (it polls _interrupt_requested every 0.2s) — stubbing it out would
    remove the mechanism under test."""
    agent = _make_agent()
    attempts: list[int] = []

    def _fake_api_call(api_kwargs):
        attempts.append(1)
        raise RateLimitError(retry_after="60")

    agent._interruptible_api_call = _fake_api_call
    _interrupt_on_diagnostic_wait(agent)

    result = agent.run_conversation("hello")

    # No zombie retries: the interrupt fired during the first backoff, so at
    # most one API attempt happened.
    assert len(attempts) <= 1, (
        f"interrupt during 429 backoff must stop the retry loop; got {len(attempts)} attempts"
    )
    # The turn must not report completion.
    assert result.get("completed") is not True


def test_429_interrupt_before_first_attempt_never_reaches_api(monkeypatch):
    """Stop before the turn starts: the 429-ing provider is never contacted."""
    agent = _make_agent()
    attempts: list[int] = []

    def _fake_api_call(api_kwargs):
        attempts.append(1)
        raise RateLimitError()

    agent._interruptible_api_call = _fake_api_call
    agent._interrupt_requested = True

    result = agent.run_conversation("hello")

    assert attempts == [], (
        "interrupted-before-start turn must not contact the API"
    )
    assert result.get("completed") is not True


@pytest.mark.parametrize("bad_retry_after", ["not-a-number", "-5"])
def test_429_garbage_retry_after_falls_through_to_backoff(monkeypatch, bad_retry_after):
    """A lying Retry-After must not crash the retry loop or wait on garbage;
    the loop falls through to bounded jittered backoff and eventually fails
    bounded — never hangs."""
    agent = _make_agent()
    attempts: list[int] = []

    def _fake_api_call(api_kwargs):
        attempts.append(1)
        raise RateLimitError(retry_after=bad_retry_after)

    agent._interruptible_api_call = _fake_api_call
    monkeypatch.setattr(
        "agent.turn_api_error.interruptible_backoff_sleep",
        lambda *a, **k: None,
    )
    # Bound the retry budget deterministically: after max retries the loop
    # must exit, not spin.
    agent.max_retries = 2

    result = agent.run_conversation("hello")

    assert 1 <= len(attempts) <= agent.max_retries + 1, (
        f"retry budget must stay bounded; got {len(attempts)} attempts"
    )
    assert result.get("completed") is not True
