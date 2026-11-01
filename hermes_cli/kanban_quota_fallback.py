"""Quota-wall merge fallback for the kanban dispatcher (t_e7827d91).

When a card's primary provider is quota-walled (its latest run ended
``rate_limited`` — exit 75), the dispatch seam can hand the claim a
``(model, provider)`` override to a configured healthy secondary lane
(e.g. merge/zai/glm-5.3-flash) so the card dispatches instead of bouncing
off the same wall every cooldown tick, and "0 workers + ready p9" reads as
provider-walled instead of an outage.

Dormant-ship rule (mirrors kanban_jev_router): the gate DEFAULTS OFF —
``providers.routers.quota_fallback.enabled: true`` activates it, or env
``HERMES_KANBAN_QUOTA_FALLBACK`` explicitly sets it (explicit env wins).
Without either, the seam returns None and dispatch keeps the static lane;
the fuel-door telemetry still records quota-walled stalls so Packet 14 GAP E
sees them while the gate is off.

Boundaries (card body): no quota raises, no new paid providers, no OAuth
subscription changes — lane-juggling within the existing fleet only. The
fallback lane list is config-declared by the operator.

Stdlib-only. Imported by name from ``hermes_cli.kanban_db_dispatch``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading as _threading
import time
from pathlib import Path
from typing import Any, Optional


# How long a rate_limited run marks a provider walled. The dispatcher cooldown
# default (300s) governs respawn pacing; the wall signal governs routing
# decisions, so it uses its own, wider window to cover back-to-back walls.
WALL_RECENT_WINDOW_SECONDS = 900

# A ready card stalled past this many seconds with zero active workers while
# its anticipated provider is walled is a dispatch-stall signal (card ask #2,
# ">180s dispatch grace").
DISPATCH_GRACE_SECONDS = 180

_reason_local = _threading.local()


def reason() -> Optional[str]:
    """Plain rationale for the most recent seam call on this thread."""
    return getattr(_reason_local, "reason", None)


def wall() -> Optional[tuple[str, str]]:
    """``(walled_model, walled_provider)`` attribution the most recent seam
    call resolved for its task — independent of whether a fallback was picked
    (the attribution exists as soon as the wall is proven, DESIGN D1). The
    dispatch seam carries this into the persisted override event so the
    full-attribution audit never drops the walled side (t_27cf3f73 repair 2)."""
    return getattr(_reason_local, "wall", None)


def _set_wall(value: Optional[tuple[str, str]]) -> None:
    _reason_local.wall = value


# ---------------------------------------------------------------------------
# Cool-down (loop guard): a card that already rode a fallback lane and got
# rate_limited again must NOT be re-routed onto a fallback until its latest
# ended run proves the walled provider is no longer the evidence. Prevents
# walled→fallback→walled wall-bounce compounding an outage.
# ---------------------------------------------------------------------------
FALLBACK_COOLDOWN_SECONDS = 300  # 5-minute cool-down after a fallback-lane run

# Default fallback fleet (lane-juggling within the existing fleet only — no
# new paid providers, no quota raises; card-body boundary). Served by the
# already-configured merge provider. Config-declared lanes
# (providers.routers.quota_fallback.fallbacks) deliberately REPLACE this
# default list — an explicit operator list is the whole list.
DEFAULT_FALLBACK_LANES = (
    {"model": "zai/glm-5.3-flash", "provider": "merge"},
    {"model": "deepseek/deepseek-v4-flash-0731", "provider": "merge"},
)

# DESIGN D2: models that exist in fleet routing but cannot serve a chat
# dispatch worker. typesafe/jev-1.13 is a decisions-only endpoint (it does not
# serve chat completions); xai/grok-4.5 rides per-minute/user-pays pricing.
# A fallback lane naming one of these is never offered to a worker.
_QUOTA_FALLBACK_EXCLUDED_MODELS = frozenset({"typesafe/jev-1.13", "xai/grok-4.5"})


def _merge_allowlist() -> Optional[list[str]]:
    """Live ``providers.merge.models`` allowlist (D2 allowlist pin), or None
    when the config cannot be read. None is never provider evidence — callers
    must fail closed (DESIGN §2 (i): a fallback lane is eligible only if the
    provider exists in config with that model's endpoint)."""
    try:
        from hermes_cli.config import read_raw_config_readonly

        cfg = read_raw_config_readonly() or {}
        merge_cfg = (cfg.get("providers") or {}).get("merge") or {}
        if not isinstance(merge_cfg, dict) or not merge_cfg:
            return None  # provider block absent = unconfigured provider, not an open gate
        models = merge_cfg.get("models")
        if models is None:
            # Provider block present but no explicit allowlist key: the
            # menu of models this provider serves is unproven. DESIGN §2
            # (i): missing allowlist evidence must fail closed (reviewer
            # verdict 1) — an unproven pin is never bypassed, and an
            # empty or missing allowlist is not a free pass to any model.
            return None
        if not isinstance(models, list):
            return None  # malformed allowlist = unprovable eligibility
        return [str(m) for m in models]
    except Exception:  # health: allow BLE001 -- unreadable config = cannot prove eligibility
        return None


def _lane_eligible(lane: dict[str, str], walled_provider_name: str) -> bool:
    """DESIGN D2 eligibility guard: may ``lane`` serve a claim walled on
    ``walled_provider_name``? Requires the provider to exist in config with
    its model endpoint (§2 (i) — missing evidence fails closed) and, for
    provider == "merge", the model must appear in the live
    ``providers.merge.models`` allowlist pin (respected, never bypassed).
    Unreadable config = cannot prove eligibility -> fail closed (False)."""
    model = str(lane.get("model") or "")
    provider = str(lane.get("provider") or "")
    if not model or not provider:
        return False
    if provider == walled_provider_name:
        return False
    if model in _QUOTA_FALLBACK_EXCLUDED_MODELS:
        return False
    if provider == "merge":
        allowed = _merge_allowlist()
        if allowed is None:
            return False  # provider unconfigured / unreadable / no pin evidence: fail closed
        if model not in allowed:
            return False  # empty or explicit pin: model must be LISTED to serve (fail closed on [])
        return True
    # §2 (i) applies to every provider, not only merge: the routing block
    # must exist in config (with that model's endpoint mentioned) before the
    # lane may serve a claim. read_raw_config_readonly returning an empty
    # dict is no provider evidence -> ineligible.
    try:
        from hermes_cli.config import _pick_provider_base_url, read_raw_config_readonly

        cfg = read_raw_config_readonly() or {}
    except Exception:  # health: allow BLE001 -- unreadable config -> no proof, no route
        return False
    provider_cfg = (cfg.get("providers") or {}).get(provider)
    return isinstance(provider_cfg, dict) and bool(_pick_provider_base_url(provider_cfg, provider))

# Never downgraded regardless of existing run attribution: the office
# quality floor (forge/chief/nova ride frontier or nothing).
_QUOTA_FALLBACK_BLOCKED_ASSIGNMENTS = frozenset({"forge", "chief", "nova"})


def _fallback_in_cooldown(conn, task) -> bool:
    """True when the task's most recent ended run rode one of the fallback
    lanes and ended within the cool-down window — the card is cooling on the
    fallback lane; re-routing it again would wall-bounce. Runs without a
    model/provider stamp are ignored (never evidence)."""
    task_id = str(getattr(task, "id", "?") or "?")
    try:
        rows = conn.execute(
            "SELECT outcome, ended_at, metadata FROM task_runs "
            "WHERE task_id = ? AND ended_at IS NOT NULL "
            "ORDER BY ended_at DESC, id DESC LIMIT 5",
            (task_id,),
        ).fetchall()
    except Exception:  # health: allow BLE001 -- fail-open: unreadable history simply re-enters the normal routing path
        return False
    lanes = {str(l["model"]) for l in _fallback_lanes()}
    now = time.time()
    for row in rows:
        ended = _epoch(row["ended_at"])
        if ended is None or (now - ended) >= FALLBACK_COOLDOWN_SECONDS:
            continue
        try:
            blob = (json.loads(row["metadata"] or "{}").get("model") or {})
            run_model = str(blob.get("model") or "")
            run_provider = str(blob.get("provider") or "")
        except Exception:  # health: allow BLE001 -- per-row stamp parse: malformed/unstamped runs are never cooldown evidence
            continue
        if run_provider and run_model in lanes:
            return True
    return False


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def fallback_enabled() -> bool:
    """Config-flag gate: ``providers.routers.quota_fallback.enabled``.

    Config-only by design (no HERMES_* env override — behaviour lives in
    config.yaml per repo policy). Unreadable config = routing stays off
    (fail-safe: an operator never gets surprise re-routing).
    """
    try:
        from hermes_cli.config import read_raw_config_readonly

        cfg = read_raw_config_readonly() or {}
        routers = ((cfg.get("providers") or {}).get("routers") or {})
        return bool((routers.get("quota_fallback") or {}).get("enabled"))
    except Exception:  # health: allow BLE001 -- deliberate fail-safe boundary: unreadable config keeps routing off, never a surprise reroute; no logger available in this seam
        return False


def _fallback_lanes() -> list[dict[str, str]]:
    """Fallback lanes in priority order: config-declared lanes, else defaults."""
    try:
        from hermes_cli.config import read_raw_config_readonly

        cfg = read_raw_config_readonly() or {}
        routers = ((cfg.get("providers") or {}).get("routers") or {})
        lanes = (routers.get("quota_fallback") or {}).get("fallbacks") or []
        if isinstance(lanes, list):
            out = []
            for lane in lanes:
                if isinstance(lane, dict) and lane.get("model") and lane.get("provider"):
                    out.append({"model": str(lane["model"]), "provider": str(lane["provider"])})
            if out:
                return out  # config-declared = the whole list
    except Exception:  # health: allow BLE001 S110 -- unreadable config serves the safe default fleet instead of erroring dispatch
        pass
    return [dict(lane) for lane in DEFAULT_FALLBACK_LANES]


# ---------------------------------------------------------------------------
# Wall detection (pure)
# ---------------------------------------------------------------------------

def latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
    """Latest ended run row for a task as a dict, or None."""
    row = conn.execute(
        "SELECT outcome, ended_at, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    meta = {}
    try:
        parsed = json.loads(row["metadata"] or "{}")
        if isinstance(parsed, dict):
            meta = parsed
    except Exception:  # health: allow BLE001 -- malformed metadata blob = no attribution, never a wall
        meta = {}
    return {"outcome": row["outcome"], "ended_at": row["ended_at"], "metadata": meta}


def _epoch(value: Any) -> Optional[float]:
    """Epoch seconds for a run ended_at timestamp column; None when unknown."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        return float(text)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            import calendar

            return calendar.timegm(time.strptime(text, fmt))
        except Exception:  # health: allow BLE001 -- legacy stamp formats are probed; non-matching = try next format
            continue
    return None


def _static_lane_for_assignee(assignee: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """DESIGN D1.3b: the assignee profile's static lane, read from its own
    config at claim time (identity-based attribution, not a guess).

    Serves the unstamped-run wall case: the run-fields claim stamp
    (D1.3a) is the primary attribution; when it is missing, the assignee's
    static lane names what the wall evidence belongs to. Read-only; never
    raises (attribution failure = fail closed, no routing, DESIGN §8)."""
    who = str(assignee or "").strip()
    if not who:
        return (None, None)
    try:
        from pathlib import Path as _Path

        from hermes_cli.profiles import _read_config_model, resolve_profile_env

        profile_dir = _Path(resolve_profile_env(who))
        model, provider = _read_config_model(profile_dir)
        if model and provider:
            return (str(model), str(provider))
        return (model, provider)
    except Exception:  # health: allow BLE001 -- unreadable profile config = no attribution; fail closed
        return (None, None)


def walled_provider(
    latest: Optional[dict[str, Any]],
    now: float,
    assignee_static_lane: Optional[tuple[Optional[str], Optional[str]]] = None,
) -> Optional[tuple[str, str]]:
    """``(model, provider)`` the latest run proved quota-walled, else None.

    The wall evidence is the run outcome (``rate_limited`` — exit 75, stamped
    without counting a failure) plus attribution resolved in DESIGN D1's
    order: a. the model/provider blob the stamp wrote at claim time
    (run-fields convention); b. when the stamp carries no provider, the
    assignee profile's static lane (``assignee_static_lane``, D1.3b) —
    attribution by identity, not guess.
    """
    if not latest or latest.get("outcome") != "rate_limited":
        return None
    ended = _epoch(latest.get("ended_at"))
    if ended is None:
        return None  # undatable = unproven; never route on it
    if (now - ended) >= WALL_RECENT_WINDOW_SECONDS:
        return None  # stale wall: window may have reset since
    model_blob = (latest.get("metadata") or {}).get("model") or {}
    model = model_blob.get("model") if isinstance(model_blob, dict) else None
    provider = model_blob.get("provider") if isinstance(model_blob, dict) else None
    if not provider:
        # DESIGN D1.3b: unstamped run — attribute the wall to the assignee's
        # static lane when one resolves; no attribution = route nothing.
        static_model, static_provider = (assignee_static_lane or (None, None))
        if static_provider:
            model = static_model if model is None else model
            provider = str(static_provider)
        else:
            return None
    return (str(model or "?"), str(provider))


def active_workers(conn: sqlite3.Connection, *, exclude_task_id: Optional[str] = None) -> int:
    """Count running rows, optionally excluding the candidate just claimed."""
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running' "
            "AND (? IS NULL OR id != ?)",
            (exclude_task_id, exclude_task_id),
        ).fetchone()[0])
    except Exception:  # health: allow BLE001 -- count failure reads as 0 workers (conservative: the fuel door may surface a stall)
        return 0


# ---------------------------------------------------------------------------
# Ledgers (walls registry + dispatch-stall telemetry, JSONL append-only)
# ---------------------------------------------------------------------------

def _ledger_dir() -> Path:
    d = Path(os.path.expandvars(r"%LOCALAPPDATA%\hermes")) / "provider-walls"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_jsonl(name: str, row: dict) -> None:
    try:
        path = _ledger_dir() / name
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:  # health: allow BLE001 S110 -- telemetry side-channel only; dispatch must never fail on a log write
        pass  # telemetry never breaks dispatch


def _stall_dedupe(conn: sqlite3.Connection, task_id: str, latest_key: str) -> bool:
    """True when this (task, latest-run) pair was already recorded.

    Dedupe key = the last line of this task's stall log for the same run key,
    so repeated guard passes on the same stranded run write exactly once.
    """
    try:
        path = _ledger_dir() / "dispatch_stall.jsonl"
        with path.open("r", encoding="utf-8-sig") as f:
            for line in reversed(f.read().splitlines()[-2000:]):
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("task_id") == task_id and row.get("run_key") == latest_key:
                    return True
    except FileNotFoundError:
        return False  # no ledger yet = clean slate: the first stall MUST be recorded
    except Exception:  # health: allow BLE001 -- unreadable ledger must NEVER double-log: suppress telemetry until readable again
        return True  # unreadable ledger — can't prove it wasn't logged, don't spam
    return False


# ---------------------------------------------------------------------------
# D3: persistent override mechanics + cleanup contract
# ---------------------------------------------------------------------------

def _persist_fallback_override(
    conn, task_id: str, model: str, provider: str,
    walled: Optional[tuple[str, str]] = None,
) -> bool:
    """DESIGN D3: persist the fallback ``(model, provider)`` on the task row
    (the same columns ``set_model_override`` writes) inside ONE write txn and
    append the ``quota_fallback_lane_picked`` event with full attribution:

    {"model", "provider", "run_id", "walled_model", "walled_provider",
     "source": "dispatch.quota_fallback"}

    so the gateway OWNER's params_policy re-read (kanban_run -> get_task)
    applies the override to the real worker turn - an in-memory patch never
    reaches the owner. On persistence failure the dispatch seam degrades to
    the recovered in-memory seam behavior (audit event with
    "persisted": False); dispatch never breaks on this path.

    ``run_id`` is read inside the same txn (claim_task opened the run just
    before the seam executes inside _dispatch_lane_task).
    """
    try:
        from hermes_cli import kanban_db as _kb
        from hermes_cli.kanban_db_connect import write_txn

        with write_txn(conn):
            run_row = conn.execute(
                "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            rid = None
            if run_row is not None and run_row["current_run_id"] is not None:
                rid = int(run_row["current_run_id"])
            payload = {
                "model": str(model),
                "provider": str(provider),
                "run_id": rid,
                "walled_model": (str(walled[0]) if walled else None),
                "walled_provider": (str(walled[1]) if walled else None),
                "source": "dispatch.quota_fallback",
            }
            conn.execute(
                "UPDATE tasks SET model_override = ?, provider_override = ? WHERE id = ?",
                (model, provider, task_id),
            )
            _kb._append_event(conn, task_id, "quota_fallback_lane_picked", payload)
        return True
    except Exception:  # health: allow BLE001 -- dispatch must never break on fallback persistence; caller degrades to in-memory only
        return False


def _newest_override_event(conn, task_id: str) -> Optional[tuple[str, Optional[dict]]]:
    """The newest override-family event as ``(kind, payload)``.

    DESIGN D3 collision rule: among {model_override_set,
    quota_fallback_lane_picked, quota_fallback_lane_cleared} the newest wins
    - a manual ``kanban set-model`` written after a fallback pick supersedes
    the automatic one and disables cleanup until cleared.
    """
    try:
        row = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind IN "
            "('model_override_set', 'quota_fallback_lane_picked', 'quota_fallback_lane_cleared') "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        payload = None
        if row["payload"]:
            try:
                parsed = json.loads(row["payload"])
                payload = parsed if isinstance(parsed, dict) else None
            except Exception:  # health: allow BLE001 -- unparseable payload = unknown authorship -> cleanup refused
                payload = None
        return (str(row["kind"]), payload)
    except Exception:  # health: allow BLE001 -- unreadable event history = authorship unknown -> cleanup refused (fail-safe)
        return None


def _walled_run_current(conn, task_id: str, now: Optional[float] = None) -> bool:
    """True when a rate_limited ended run still sits inside the wall window
    (DESIGN D3 cleanup precondition 3: the wall must have aged out >=900s
    past the last walled attribution before the fallback override clears)."""
    try:
        now = time.time() if now is None else now
        rows = conn.execute(
            "SELECT ended_at FROM task_runs "
            "WHERE task_id = ? AND outcome = 'rate_limited' AND ended_at IS NOT NULL",
            (task_id,),
        ).fetchall()
        for row in rows:
            ended = _epoch(row["ended_at"])
            if ended is None:
                continue
            if (now - ended) < WALL_RECENT_WINDOW_SECONDS:
                return True
    except Exception:  # health: allow BLE001 -- unreadable history = cannot prove aged-out -> keep override (fail-safe)
        return True
    return False


def _would_cleanup_fallback(
    conn, task_id: str, current_model: Optional[str], current_provider: Optional[str],
) -> bool:
    """DESIGN D3 cleanup contract (all four must hold):

    1. newest override-family event is quota_fallback_lane_picked
       (fallback-authored; a manual model_override_set is always respected);
    2. the pick payload still matches the live override on the task row
       (the override on the row is ours to clean, not an operator write);
    3. no rate_limited run inside the wall window (wall aged out);
    4. the latest ended run is not rate_limited (fallback lane proved
       healthy or the card completed since).
    """
    newest = _newest_override_event(conn, task_id)
    if newest is None or newest[0] != "quota_fallback_lane_picked":
        return False
    payload = newest[1] or {}
    if not current_model or str(current_model) != str(payload.get("model") or ""):
        return False
    if str(current_provider or "") != str(payload.get("provider") or ""):
        return False
    if _walled_run_current(conn, task_id):
        return False
    latest = latest_run(conn, task_id)
    if latest and latest.get("outcome") == "rate_limited":
        return False
    return True


def _cleanup_fallback_override(conn, task_id: str) -> bool:
    """DESIGN D3 cleanup: clear a FALLBACK-authored override when the D3
    contract holds (newest override event still ours, wall aged out, latest
    run not rate_limited) in ONE write txn, emitting the
    ``quota_fallback_lane_cleared`` audit event. Returns True when cleared.

    Fail-safe: any error = keep the override (never lose operator routing).
    """
    try:
        from hermes_cli import kanban_db as _kb
        from hermes_cli.kanban_db_connect import write_txn

        with write_txn(conn):
            row = conn.execute(
                "SELECT model_override, provider_override FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            cur_model = row["model_override"]
            cur_provider = row["provider_override"]
            if not cur_model or not _would_cleanup_fallback(conn, task_id, cur_model, cur_provider):
                return False
            conn.execute(
                "UPDATE tasks SET model_override = NULL, provider_override = NULL WHERE id = ?",
                (task_id,),
            )
            _kb._append_event(
                conn, task_id, "quota_fallback_lane_cleared",
                {"source": "dispatch.quota_fallback",
                 "cleared_model": str(cur_model or ""),
                 "cleared_provider": str(cur_provider or "")},
            )
        return True
    except Exception:  # health: allow BLE001 -- cleanup is best-effort; failure keeps the override (fail-safe)
        return False


# ---------------------------------------------------------------------------
# Claim-time seam
# ---------------------------------------------------------------------------

def claim_lane(
    conn: sqlite3.Connection,
    task: Any,
    lane: str = "ready",
    inject_now: Optional[float] = None,
) -> Optional[tuple[str, str]]:
    """The one call the dispatch seam makes per claim.

    Returns ``(model, provider)`` to run the claim on instead of the profile's
    static lane, or None to keep the static lane. Distinct behaviors:

    - board ``model_override`` present -> None (board override always wins);
    - assignee in the no-downgrade floor (forge/chief/nova) -> None;
    - gate off -> None, but the fuel door still records a quota-walled stall
      (ready task + 0 active workers past the dispatch grace) so GAP E
      telemetry is complete;
    - task cooling down on a fallback lane -> None (prevents wall-bounce);
    - walled primary + healthy configured fallback -> the first fallback lane
      served by a provider not itself walled.
    """
    task_id = str(getattr(task, "id", "?") or "?")
    now = inject_now if inject_now is not None else time.time()

    # D3 cleanup contract (opportunistic): when this claim does NOT need the
    # override anymore (wall aged out + newest override event still a
    # fallback pick + latest run not rate_limited), clear the stale
    # fallback override with its quota_fallback_lane_cleared audit event
    # before computing anything else. Gated on the module gate so the
    # cleanup contract is reachable only when routing is active at all.
    # Repair 3 (t_27cf3f73): after a real DB clear, re-read the row and
    # synchronize the claimed Task's routing fields so the object the
    # dispatcher carries forward agrees with the persisted board state —
    # claim-time stamping and spawn/owner re-reads must never see a stale
    # fallback override the DB no longer holds. Manual overrides are never
    # touched (cleanup refuses operator-authored overrides by contract).
    if fallback_enabled():
        if _cleanup_fallback_override(conn, task_id):
            try:
                refreshed = conn.execute(
                    "SELECT model_override, provider_override FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if refreshed is not None:
                    task.model_override = refreshed["model_override"]
                    task.provider_override = refreshed["provider_override"]
            except Exception:  # health: allow BLE001 -- resync is best-effort; the DB clear is the authority
                pass

    latest = latest_run(conn, task_id)
    static_lane = _static_lane_for_assignee(getattr(task, "assignee", None))
    walled = walled_provider(latest, now, assignee_static_lane=static_lane)
    _set_wall(walled)  # D1 attribution published even when no fallback is picked
    run_key = f"{latest.get('ended_at') if latest else ''}" if walled else ""

    if str(getattr(task, "assignee", "") or "") in _QUOTA_FALLBACK_BLOCKED_ASSIGNMENTS:
        _reason_local.reason = "assignee no-downgrade floor"
        return None

    if bool(getattr(task, "model_override", None)):
        _reason_local.reason = "board model_override kept"
        return None

    if walled:
        walled_model, walled_provider_name = walled
        # Fuel door (card ask #2): a card stranded on a walled provider with
        # the office at zero workers past the dispatch grace. idle_seconds =
        # staleness of the walled run (cooldown-tagged to that run; repeats
        # refresh it). A walled claim during normal dispatch traffic is NOT
        # a stall — it's the first requeue tick of an understood cooldown.
        # claim_task already marked this candidate running before this hook;
        # only OTHER running tasks are evidence of existing dispatch traffic.
        stalled = active_workers(conn, exclude_task_id=task_id) == 0 and (
            now - (_epoch(latest.get("ended_at")) or now)
        ) > DISPATCH_GRACE_SECONDS
        if stalled and not _stall_dedupe(conn, task_id, run_key):
            _append_jsonl("dispatch_stall.jsonl", {
                "ts": now,
                "task_id": task_id,
                "run_key": run_key,
                "lane": lane,
                "reason": "primary_provider_quota_walled",
                "walled_model": walled_model,
                "walled_provider": walled_provider_name,
                "active_workers": 0,
                "idle_seconds": round(now - (_epoch(latest.get("ended_at")) or now), 1),
                "stall_signal": "ready_with_zero_workers_on_walled_provider",
                "gap_e": "kanban_dispatch_stall",
            })

        # Cool-down loop guard: the card last rode a fallback lane and that
        # run just ended — hold this requeue to the static lane instead of
        # compounding the bounce.
        if _fallback_in_cooldown(conn, task):
            _reason_local.reason = "fallback cooldown hold"
            return None

    if not fallback_enabled():
        _reason_local.reason = "gate off" if not walled else "gate off (primary walled, telemetry recorded)"
        return None

    if not walled:
        _reason_local.reason = "primary healthy"
        return None

    lanes = _fallback_lanes()

    for fallback in lanes:
        if _lane_eligible(fallback, walled_provider_name):
            _reason_local.reason = (
                f"walled {walled_provider_name}/{walled_model} -> fallback {fallback['provider']}/{fallback['model']}"
            )
            return (fallback["model"], fallback["provider"])

    _reason_local.reason = "no eligible fallback lane configured"
    return None
