"""``hermes pause`` / ``hermes resume`` — global and automatic Office pauses.

``pause`` writes the ESTOP sentinel at ``$HERMES_HOME/ESTOP``; cron, kanban and new gateway
turns halt on their next check (in-flight work is never killed). Every local turn is a gateway
turn, so interactive CLI, TUI and Desktop turns are held too, with ``agent.estop.paused_reply``'s
notice. ``resume`` removes it and operation resumes on the next tick — no restart. Ported from gastownhall/gastown estop.go (MIT).
``--office-only`` holds cron, Kanban, heartbeat and loop dispatch independently,
so a global ESTOP can be lifted without restarting automatic work.
"""

from __future__ import annotations

import argparse


def cmd_pause(args: argparse.Namespace) -> int:
    """Engage the global emergency stop."""
    from agent.estop import (
        engage, get_state, is_engaged, office_engage, office_is_engaged,
    )

    reason = getattr(args, "reason", None)
    if getattr(args, "office_only", False):
        already = office_is_engaged()
        path = office_engage(reason=reason)
        print(f"⏸️  Office dispatch {'remains paused' if already else 'paused'}")
        print(f"    sentinel: {path}")
        print("    Cron, Kanban, and automatic Office dispatch are held; new interactive turns remain available.")
        return 0
    already = is_engaged()
    path = engage(reason=reason)
    state = get_state() or {}
    verb = "Still paused" if already else "Hermes paused"
    detail = f" — reason: {state['reason']}" if state.get("reason") else ""
    print(f"⏸️  {verb}{detail}")
    print(f"    sentinel: {path}")
    print(
        "    Cron dispatch, kanban dispatch, and new gateway turns (messaging and interactive\n"
        "    CLI, TUI and Desktop turns alike) are on hold. In-flight work keeps running.\n"
        "    Run `hermes resume` to lift the pause.")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Disengage the global emergency stop."""
    from agent.estop import disengage, sentinel_path, office_disengage, office_is_engaged

    if getattr(args, "office_only", False):
        if office_disengage():
            print("▶️  Office dispatch pause lifted; automatic work may resume on the next tick.")
        else:
            print("Office dispatch was not paused.")
        return 0

    if disengage():
        if office_is_engaged():
            print("▶️  Global pause lifted; Office dispatch remains paused.")
        else:
            print("▶️  Hermes resumed — dispatch picks up on the next tick.")
    else:
        print(f"Hermes is not paused (no sentinel at {sentinel_path()}).")
    return 0


def build_pause_parser(subparsers) -> None:
    """Attach the ``pause`` and ``resume`` subcommands to ``subparsers``."""
    pause_parser = subparsers.add_parser(
        "pause", help="Emergency stop: pause cron/kanban dispatch and new gateway turns",
        description="Engage the global emergency stop. Halts NEW work only — cron "
            "dispatch, kanban dispatch, and new gateway turns — until "
            "`hermes resume`. Interactive CLI, TUI and Desktop turns are gateway turns, so they "
            "are held too: each gets \"Hermes is paused\" with the reason. Turns already "
            "running finish; in-flight work is never killed.")
    pause_parser.add_argument(
        "--reason", default=None, help="Optional reason stored in the sentinel and shown to users")
    pause_parser.add_argument(
        "--office-only", action="store_true",
        help="Pause automatic cron, Kanban, heartbeat and loop dispatch without blocking interactive turns")
    pause_parser.set_defaults(func=cmd_pause)

    resume_parser = subparsers.add_parser(
        "resume", help="Lift the emergency stop set by `hermes pause`",
        description="Remove the ESTOP sentinel; dispatch resumes on the next tick.")
    resume_parser.add_argument(
        "--office-only", action="store_true", help="Lift only the Office dispatch pause")
    resume_parser.set_defaults(func=cmd_resume)
