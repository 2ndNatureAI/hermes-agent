"""Pending-tool-tail replay retry (t_9bd2e446, stoplevel #55316 sibling).

The finalizer's ``pending_tool_result`` seam fails a turn that ended on a raw tool row
with no follow-up text. Interactive users type ``continue``; cron and kanban workers
cannot. ``agent.turn_facade`` now replays the tail as the next turn when the same agent
still holds iteration budget (cap 2 per parent turn). Budget-exhausted tails keep
today's failure so the worst case is exactly the pre-patch behavior.

The facade seeds its replay from the just-finished transcript and never calls
into a nested hostile agent instance. The loop dependency is patched at its
source module; the budget regression calls the real turn prologue through that
offline wrapper, while the other tests check facade dispatch contracts.

Call-contract notes (verified against the live facade, attempt-11 probe):
- The ORIGINAL turn and every retry invoke the loop dependency; "no retry" cases
  therefore record exactly ONE loop call, not zero.
- The original call passes ``task_id`` POSITIONALLY; the retry passes it as a keyword
  arg. Both must carry the same value. The retry takes the prior result's tool row and
  appends a visible ``continue`` turn.
- Relay finalization for a turn that ends failed-with-retries lands on ``end_turn``
  with outcome ``failed`` once; a recovered turn ends ``success``.
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

# Resolve every string-target module at COLLECTION time: the guarded real-home
# fixture winds monkeypatch up per test, and pytest's string-target resolution
# (``monkeypatch.setattr("agent.conversation_loop.run_conversation", ...)``)
# then imports modules under the active guard — hermes_bootstrap's PM
# activation reads the real manifest.json and trips the refusal. Module-scope
# imports run at collection, where the same imports pass.
import agent.conversation_loop as conversation_loop_module
import agent.relay_runtime
import hermes_cli.observability.relay_shared_metrics
from tests.agent.test_turn_context import _FakeAgent as _ContextAgent, _build

HK = "test_turn_facade_pending_tool"


class _FakeRelay:
    """Session-coordinator shim whose turn objects opt out of relay task metrics."""

    def __init__(self):
        self.calls: list = []

    def acquire_conversation(self, **kwargs):
        self.calls.append("acquire")
        return object()

    def begin_turn(self, lease, **kwargs):
        self.calls.append("begin")
        return SimpleNamespace(relay_enabled=False)

    def finish_logical_calls(self, turn, outcome=None):
        self.calls.append(("finish", outcome))

    def end_turn(self, turn, outcome=None):
        self.calls.append(("end", outcome))

    def release_conversation(self, lease):
        self.calls.append("release")

    def register_session_initializer(self, *args, **kwargs):
        pass


def _agent(remaining: int = 3) -> SimpleNamespace:
    """The only agent surface the facade touches when the loop and lease are faked."""
    return SimpleNamespace(
        session_id="s1",
        platform="cron",
        model="m",
        _session_db=None,
        _relay_pending_turn_id=None,
        iteration_budget=SimpleNamespace(remaining=remaining),
        _conversation_root_id=lambda: "root",
    )


def _pending_result(api_calls: int = 36) -> dict:
    return {
        "final_response": "",
        "messages": [
            {"role": "user", "content": "Business follow-up prompt"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1", "function": {"name": "lookup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "customer follow-up is due"},
            {"role": "assistant", "content": "No reply: pending tool result"},
        ],
        "api_calls": api_calls,
        "completed": False,
        "turn_exit_reason": "pending_tool_result",
        "failed": True,
        "interrupted": False,
    }


def _success_result() -> dict:
    return {
        "final_response": "answered the pending tool result",
        "messages": [],
        "api_calls": 38,
        "completed": True,
        "turn_exit_reason": "",
        "failed": False,
        "interrupted": False,
    }


@pytest.fixture()
def facade_env(monkeypatch):
    """Patch every function-local facade dependency at its source module; return the
    captured fake-loop calls."""
    loop_calls: list = []

    def _fake_loop(agent, *args, **kwargs):
        payload = {"agent": agent, "args": args, "kwargs": kwargs}
        loop_calls.append(payload)
        responses = kwargs.pop("_responses", None) or agent._responses  # test hook
        idx = min(len(loop_calls) - 1, len(responses) - 1)
        return dict(responses[idx])

    monkeypatch.setattr(
        "agent.conversation_loop.run_conversation", _fake_loop, raising=True)
    monkeypatch.setattr(
        "agent.background_review.cancel_background_review_for_live_turn",
        lambda agent: None, raising=True)
    monkeypatch.setattr("agent.interrupt_scope.track_in_interrupt_scope",
                        lambda agent: nullcontext(), raising=True)
    monkeypatch.setattr("agent.auxiliary_client.scoped_runtime_main",
                        lambda scope: nullcontext(), raising=True)
    relay = _FakeRelay()
    import agent.relay_runtime as relay_runtime_module

    monkeypatch.setattr(relay_runtime_module, "SESSION_COORDINATOR", relay,
                        raising=True)
    import hermes_cli.observability.relay_shared_metrics as metrics

    monkeypatch.setattr(metrics, "start_task_run", lambda **k: None, raising=True)
    monkeypatch.setattr(metrics, "finish_task_run", lambda **k: None, raising=True)

    def _drive(agent_resp, agent=None):
        agent = agent or _agent()
        agent._responses = agent_resp  # read by the fake loop
        from agent.turn_facade import TurnFacadeMixin

        result = TurnFacadeMixin.run_conversation(
            agent, "Business follow-up prompt", system_message="sys",
            conversation_history=[{"role": "user", "content": "Business follow-up prompt"}],
            task_id="t1",
        )
        return agent, result, loop_calls, relay

    return _drive


def _assert_call_payloads(loop_calls, agent):
    """Each retry carries the just-finished tool result, not the stale admission history."""
    for index, call in enumerate(loop_calls):
        assert call["agent"] is agent  # same instance: session intact, same lease
        assert call["args"][0] == ("Business follow-up prompt" if index == 0 else "continue")
        if index:
            assert call["args"][2][-2]["role"] == "tool"
            assert call["args"][2][-2]["content"] == "customer follow-up is due"
        shipped = call["kwargs"].get("task_id")
        if shipped is None and len(call["args"]) > 3:
            shipped = call["args"][3]
        assert shipped == "t1"


def test_pending_tool_tail_is_replayed_until_the_model_answers(facade_env):
    """Gate open + budget left → the tail replays as the next turn, up to 2 retries."""
    agent, result, loop_calls, relay = facade_env(
        [_pending_result(), _pending_result(api_calls=37), _success_result()])
    assert result["completed"] is True
    assert result["final_response"] == "answered the pending tool result"
    assert result["failed"] is False
    assert len(loop_calls) == 3  # original turn + 2 bounded retries (cap honored)
    _assert_call_payloads(loop_calls, agent)
    assert relay.calls[0] == "acquire"
    assert relay.calls[-1] == "release"
    final_ends = [c for c in relay.calls if isinstance(c, tuple) and c[0] == "end"]
    assert final_ends[-1][1] == "success"  # the retry recovered the turn


def test_retry_gate_stays_open_across_the_second_pending_tail_only(facade_env):
    """Two retries exhaust the per-parent-turn cap even when both retries still fail."""
    agent, result, loop_calls, relay = facade_env(
        [_pending_result(), _pending_result(api_calls=37), _pending_result(api_calls=38)])
    assert result["failed"] is True
    assert result["turn_exit_reason"] == "pending_tool_result"
    assert len(loop_calls) == 3  # cap = 2 retries, never a third
    _assert_call_payloads(loop_calls, agent)
    assert ("end", "failed") in relay.calls


def test_no_retry_when_budget_is_exhausted(facade_env):
    """budget.remaining == 0 keeps the pre-patch contract: the failed tail is returned."""
    agent = _agent(remaining=0)
    _, result, loop_calls, _ = facade_env([_pending_result()], agent=agent)
    assert result["failed"] is True
    assert result["turn_exit_reason"] == "pending_tool_result"
    assert len(loop_calls) == 1  # original turn only: the gate refuses the replay


def test_no_retry_when_turn_is_not_failed(facade_env):
    _, result, loop_calls, _ = facade_env(
        [{"failed": False, "turn_exit_reason": "pending_tool_result",
          "final_response": "", "messages": [], "completed": False,
          "interrupted": False}])
    assert result["failed"] is False
    assert len(loop_calls) == 1  # a non-failed tail is answered as-is, no replay


def test_no_retry_for_other_exit_reasons(facade_env):
    _, result, loop_calls, _ = facade_env(
        [{"failed": True, "turn_exit_reason": "max_iterations_reached(36/150)",
          "final_response": "", "messages": [], "completed": False,
          "interrupted": False}])
    assert result["failed"] is True
    assert len(loop_calls) == 1  # only the pending_tool_result seam earns a replay


def test_no_retry_when_interrupted(facade_env):
    _, _result, loop_calls, _ = facade_env(
        [{"failed": True, "turn_exit_reason": "pending_tool_result", "interrupted": True,
          "final_response": "", "messages": [], "completed": False}])
    assert len(loop_calls) == 1  # an interrupt is a user stop: never auto-replay it


def test_missing_budget_object_never_retries(facade_env):
    """Conservative: an agent without an IterationBudget keeps today's failure."""
    agent = _agent()
    agent.iteration_budget = None
    _, result, loop_calls, _ = facade_env([_pending_result()], agent=agent)
    assert result["failed"] is True
    assert len(loop_calls) == 1  # original turn only: no budget means no replay


from agent.turn_facade import _should_retry_pending_tool_tail


def test_gate_helper_rejects_malformed_results():
    """Unit-level guard rails on the gate itself (the facade tests cover the wire-up)."""
    assert _should_retry_pending_tool_tail(_agent(), None) is False
    assert _should_retry_pending_tool_tail(_agent(), "nope") is False
    agent = _agent()
    agent.iteration_budget = None
    assert _should_retry_pending_tool_tail(agent, _pending_result()) is False
    assert _should_retry_pending_tool_tail(_agent(), _pending_result()) is True
    malformed = _pending_result()
    malformed["messages"] = [{"role": "user", "content": "old request"}]
    assert _should_retry_pending_tool_tail(_agent(), malformed) is False


@pytest.mark.parametrize(
    "mode,consumption,expected_calls",
    [
        ("recovered", (17, 5, 7), 3),
        ("depleted", (90,), 1),
        ("interrupted", (17,), 1),
        ("retry_cap", (17, 5, 7), 3),
    ],
)
def test_real_turn_prologue_preserves_bounded_parent_budget(
    facade_env, monkeypatch, mode, consumption, expected_calls,
):
    """The internal retry crosses the real build_turn_context budget reset seam."""
    agent = _ContextAgent()
    agent.platform = "cron"
    agent._session_db = None
    agent._relay_pending_turn_id = None
    agent._conversation_root_id = lambda: "root"
    agent.run_budget_seconds = 300
    observed = []
    monkeypatch.setattr("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None)

    def loop(current, user_message, system_message=None, conversation_history=None, *args, **kwargs):
        ctx = _build(
            current, user_message=user_message, conversation_history=conversation_history,
            pending_tail_continuation=kwargs.get("pending_tail_continuation"),
        )
        observed.append((current.iteration_budget, current.iteration_budget.remaining,
                         current._run_budget_started_at, ctx.messages))
        for _ in range(consumption[len(observed) - 1]):
            assert current.iteration_budget.consume()
        if mode == "recovered" and len(observed) == 3:
            return _success_result()
        pending = _pending_result()
        if mode == "interrupted":
            pending["interrupted"] = True
        return pending

    monkeypatch.setattr(conversation_loop_module, "run_conversation", loop)
    _, result, _, _ = facade_env([_pending_result()], agent=agent)

    assert len(observed) == expected_calls
    assert observed[0][1] == 90
    assert agent.iteration_budget.remaining == 90 - sum(consumption)
    if expected_calls > 1:
        assert [row[1] for row in observed] == [90, 73, 68]
        assert all(row[0] is observed[0][0] for row in observed)
        assert all(row[2] == observed[0][2] for row in observed)
        assert all(any(message.get("role") == "tool" and
                       message.get("content") == "customer follow-up is due"
                       for message in row[3]) for row in observed[1:])
        assert all(row[3][-1]["content"] == "continue" for row in observed[1:])
    assert result["completed"] is (mode == "recovered")
    assert result["interrupted"] is (mode == "interrupted")
