"""Per-turn resolved model/provider state + token usage accounting that kanban
completion handlers can auto-stamp onto the run's metadata blob without every
call site passing them explicitly.

A contextvar is the right shape here (not a module global, not thread-local):
the agent loop may hop providers mid-turn inside a single thread, and
delegate_task children each get their own context, so a plain global or
thread-local would read the wrong process-wide value at dispatch time.

Set once at turn start by the agent for the primary route; reset at turn end.
Defaults to ``None`` so callers that cannot read it (cron jobs, non-agent
invokers) see nothing stamped and the field stays NULL — no schema migration,
no disruption to running workers.

Usage accounting mirrors ``agent._USAGE_STATE`` (session_prompt_tokens,
session_completion_tokens, session_total_tokens, session_api_calls,
session_estimated_cost_usd) at the moment the turn finalizes, so the run's
metadata carries the actual tokens/cost the worker burned rather than a
configured guess.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

#: Resolved primary route for the current turn: {"model": ..., "provider": ...,
#: "base_url": ..., "reasoning_effort": ..., "is_fallback": bool, ...}.
#: Set by the agent at turn start; None when no agent context is active.
current_resolved_state: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "current_resolved_state", default=None
)

#: Per-turn token/cost snapshot captured at finalization time. Built by
#: :func:`freeze_usage_snapshot` from the agent's accumulated usage state.
current_usage_snapshot: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "current_usage_snapshot", default=None
)


def freeze_usage_snapshot(agent: Any) -> dict[str, Any]:
    """Best-effort token/cost snapshot from *agent* at finalization time.

    Reads whatever the agent has accumulated so far (``session_*_tokens``,
    ``session_api_calls``, ``session_estimated_cost_usd``). Missing or
    non-numeric fields are treated as zero — this is a best-effort stamp, not
    a billing-grade audit record.
    """
    def _int(val: Any, default: int = 0) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _float(val: Any, default: float = 0.0) -> float:
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    return {
        "input_tokens": _int(getattr(agent, "session_input_tokens", None)
                              or getattr(agent, "session_prompt_tokens", None)),
        "output_tokens": _int(getattr(agent, "session_output_tokens", None)
                               or getattr(agent, "session_completion_tokens", None)),
        "total_tokens": _int(getattr(agent, "session_total_tokens", None)),
        "api_calls": _int(agent.session_api_calls) if hasattr(agent, "session_api_calls") else 0,
        "cost_usd": _float(getattr(agent, "session_estimated_cost_usd", None)),
    }
