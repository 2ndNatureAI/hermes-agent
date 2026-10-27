"""Real-profile assignee guard for the kanban board.

Why
---
The dispatcher classifies an unreal assignee as "skipped_nonspawnable" and,
for control-plane lanes that pull via "claim_task", deliberately does nothing
else. Correct for genuine control-plane names - but a typically real-shaped
name (hermes, author, sage) that does not resolve to a profile leaves cards
waiting in ready with zero evidence on any paging surface. Incident
t_c28d32f8 (2026-10-22): a priority-9 founder brief assigned to the
non-existent "hermes" profile sat ready 23 hours unnoticed.

Scope: input validation + observability only. No dispatch policy change.
Not in scope: phantom task-id validation (kanban_db phantom-gate), alias
collisions, the 0-max-runtime audit (separate card class).

Why this module and not profiles.py
-----------------------------------
profiles.py is imported by nearly every CLI entry point; its shared code must
stay import-light and fail-open. This helper is board-facing protection with
two callers (kanban_db.py create/reassign, kanban_db_dispatch.py beside the
nonspawnable bucket).

Why fail-open
-------------
Guard failures must never disable more of the board than the assignee field.
Every helper returns its fail-open value on unexpected error and lets the
caller log the degradation.

Operator override: NOT implemented. Unset-by-default behavior is warn-only;
no config key can currently disable the warnings. If silence is truly
needed, fix the profile instead.
"""

from __future__ import annotations

import logging
from typing import Optional

_log = logging.getLogger(__name__)


def snap(assignee: str) -> str:
    """One-call seam helper: shape-validate + resolution-warn, return the token."""
    result = validate_assignee(assignee, location="seam")
    warn_unknown_assignee(result, location="seam")
    return result


# Names that are not profile directories but have operator meaning and stay
# legal everywhere validation runs.
CONTROL_PLANE_ASSIGNEES = frozenset({
    "default",     # operator fallback / unassigned-pending dispatch
    "josh",        # founder-owned card
    "peer",        # cross-peer handoff marker
    "dispatcher",  # dispatcher-owned control-plane card
    "reviewer",    # review-lane generic assignment
})
# Callers use the public name; keep the private alias for the module's own
# internal reads so grep stays unambiguous.
_CONTROL_PLANE_ASSIGNEES = CONTROL_PLANE_ASSIGNEES


def profile_assignee_names() -> Optional[frozenset]:
    """Set of legal assignee names, or None when enumeration fails (fail-open).

    None tells the caller to treat any name as legal: a broken helper may
    degrade VALIDATION to the old permissive behavior, never degrade
    dispatch. Enumerated per call: one directory listing, picks up profile
    activation/rename without a process restart.
    """
    try:
        from hermes_cli import profiles
        named = {p.name for p in profiles.list_profiles(lazy_skill_count=True)}
    except Exception as exc:
        _log.debug("kanban assignee guard: profile enumeration failed (%s)", exc)
        return None
    allowed = set(named) | set(_CONTROL_PLANE_ASSIGNEES)
    allowed.add("default")  # list_profiles omits the default profile name
    return frozenset(allowed)


def validate_assignee(assignee: Optional[str], *, location: str) -> str:
    """Token-shape validation on every assignee write seam.

    Raises ValueError when the assignee is not a plain single token (spaces or
    path separators - the dispatcher could never spawn it, and this shape is
    the root cause class for the #106342-style silent stranding). Returns the
    normalized token otherwise. Fail-open: shape errors are the only raise;
    name-resolution quality is the WARN lane in warn_unknown_assignee.
    """
    if not assignee:
        return ""
    if not isinstance(assignee, str):
        raise ValueError(
            f"kanban {location}: assignee must be a string, got "
            f"{type(assignee).__name__}")
    s = assignee.strip()
    if not s:
        return ""
    if any(ch.isspace() for ch in s) or "/" in s:
        raise ValueError(
            f"kanban {location}: assignee {assignee!r} must be a single token "
            "without spaces or slashes")
    return s


def warn_unknown_assignee(assignee: Optional[str], *, location: str,
                          profile_names: Optional[frozenset] = None) -> None:
    """Warn when the assignee does not resolve to a live profile name.

    Shape errors log a warning (never raise) because triage flows legitimately
    create cards with empty/placeholder assignees for later assignment; the
    dispatcher's skipped_nonspawnable event + per-tick readback covers the
    visible-evidence gap. Profile-resolution misses warn with the live
    profile list echoed. Fail-open: enumeration failure (None) means no
    warning this call.
    """
    if not assignee:
        return
    if not isinstance(assignee, str) or assignee.strip() != assignee or any(
            ch.isspace() for ch in assignee) or "/" in assignee:
        _log.warning(
            "kanban %s: unusable assignee shape %r (single token required "
            "- no spaces, no slash)", location, assignee)
        return
    names = profile_names if profile_names is not None else profile_assignee_names()
    # Resolution authority = the dispatcher's own seam: a name the dispatcher
    # can spawn (profile_exists true) must never be warned about, or the two
    # layers could disagree on the same card.
    try:
        from hermes_cli import profiles

        if profiles.profile_exists(assignee):
            return
    except Exception:
        pass  # fail-open below: fall back to the set comparison
    if names is not None and assignee not in names:
        if assignee in _CONTROL_PLANE_ASSIGNEES:
            # Not a profile, but deliberate: has operator meaning and the
            # dispatcher reads it as pull-via-claim_task, never spawn.
            # Warning about 'josh' every write would be noise, not signal.
            return
        real = sorted(n for n in names if n not in _CONTROL_PLANE_ASSIGNEES)
        _log.warning(
            "kanban %s: assignee %r is not a live profile on this host. "
            "Will not spawn. Valid profiles: %s", location, assignee, ", ".join(real))
