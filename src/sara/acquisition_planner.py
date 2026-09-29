"""Deterministic dossier-driven acquisition planning.

The planner reads the persisted dossier assessment for one entity and
decides the single next bounded acquisition action, or stops. It never
mutates facts, observations, or evidence: planning is a pure decision
over assessment state, and the only persisted output is the decision
record itself.

Determinism contract: for the SAME entity, the SAME sealed assessment,
the SAME session history snapshot, and the SAME planner inputs
(retry-window start, cooldown horizon, decisions-taken counter, and the
decision ceiling), the same policy version yields the same decision id
and the same chosen action or stop. The session-history snapshot and the
operational inputs are sealed INTO the decision id and persisted in the
record, so two runs that differ in any of them are visibly different
decisions rather than silently divergent ones.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

PLANNER_POLICY_VERSION = "acquisition-planner-v2"

#: Domain states that satisfy the assessment's readiness bar.
READY_STATES = frozenset({"sufficient", "strong", "not_applicable"})

#: The single allowlisted acquisition action. v1 knows one collector.
ACTIONS = {
    "acquire_official_website": {
        "collector": "sara.website",
        "improves_domains": frozenset({
            "digital_presence", "digital_capabilities", "communication",
            "offerings", "business_model",
        }),
    },
}

#: Deficiencies the allowlisted actions cannot address. Deciding on one
#: of these stops the cycle rather than mapping it to an unsupported
#: collector.
UNSUPPORTED_DOMAINS = frozenset({
    "competitive_context", "customer_journey", "customer_market",
    "identity", "locations", "classification", "reputation",
    "marketing", "technology", "people", "operations", "change",
    "scale", "unknowns", "provenance",
})

STOP_SUFFICIENT = "sufficient_state"
STOP_UNSUPPORTED = "unsupported_deficiency"
STOP_RETRIES = "retry_ceiling"
STOP_COOLDOWN = "cooldown_active"
STOP_POLICY = "policy_ceiling"
STOP_NO_ASSESSMENT = "no_persisted_assessment"
STOP_INTEGRITY = "assessment_integrity"

#: Default retry window: partial sessions older than this do not count
#: toward the retry ceiling (7 days, in seconds).
RETRY_WINDOW_SECONDS = 7 * 24 * 3600

#: Default cooldown horizon: blocked/failed sessions older than this no
#: longer hold the entity in cooldown (24 hours, in seconds).
COOLDOWN_SECONDS = 24 * 3600

_RETRY_CEILING = 2
_COOLDOWN_SESSIONS = 3


@dataclass(frozen=True)
class PlannerDecision:
    """One planner decision cycle outcome."""

    action: str | None
    stop_reason: str | None
    reason_code: str
    target_domain: str | None
    policy_version: str = PLANNER_POLICY_VERSION
    decision_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.action is None) == (self.stop_reason is None):
            raise ValueError("a decision carries exactly one action or one stop reason")


def _instant(value: str) -> datetime:
    """Parse a persisted ISO timestamp to a UTC instant."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {value!r} is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _session_history(conn: sqlite3.Connection, entity_id: str, *, now: str) -> dict[str, Any]:
    """Snapshot the website session facts the decision depends on.

    Timestamps are compared as normalized UTC instants, never lexically.
    Only sessions inside the retry window count as partial retries; only
    blocked/failed sessions inside the cooldown horizon hold cooldown.
    """
    now_dt = _instant(now)
    retry_floor = datetime.fromtimestamp(
        now_dt.timestamp() - RETRY_WINDOW_SECONDS, tz=timezone.utc
    )
    cooldown_floor = datetime.fromtimestamp(
        now_dt.timestamp() - COOLDOWN_SECONDS, tz=timezone.utc
    )
    rows = conn.execute(
        "SELECT status, started_at FROM acquisition_sessions "
        "WHERE target_subject_id=? AND collector_name='sara.website' "
        "ORDER BY started_at DESC",
        (entity_id,),
    ).fetchall()
    streak = 0
    for status, started_at in rows:
        if status in ("blocked", "failed"):
            streak += 1
        else:
            break
    streak_recent = 0
    for status, started_at in rows:
        if status not in ("blocked", "failed"):
            break
        try:
            when = _instant(str(started_at))
        except ValueError:
            streak_recent = streak  # corrupt timestamp: fail closed to full streak
            break
        if when >= cooldown_floor:
            streak_recent += 1
        else:
            break
    partials_in_window = 0
    for status, started_at in rows:
        if status != "partial":
            continue
        try:
            when = _instant(str(started_at))
        except ValueError:
            partials_in_window += 1  # corrupt timestamp: fail closed
            continue
        if when >= retry_floor:
            partials_in_window += 1
    return {
        "session_count": len(rows),
        "blocked_failed_streak_total": streak,
        "blocked_failed_streak_in_cooldown_horizon": streak_recent,
        "partial_sessions_in_retry_window": partials_in_window,
        "retry_window_seconds": RETRY_WINDOW_SECONDS,
        "cooldown_horizon_seconds": COOLDOWN_SECONDS,
    }


def _decision_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "plan_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:40]


def plan_next_acquisition(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    now: str,
    decisions_taken: int = 0,
    max_decisions: int = 25,
) -> PlannerDecision:
    """Decide the single next action for one entity from its persisted assessment.

    ``now`` is the decision clock (ISO-8601, timezone-aware). The retry
    window and cooldown horizon are fixed policy constants derived from
    ``now``; every operational input is sealed into the decision id.
    """
    from .dossier.status import persisted_assessment

    history = _session_history(conn, entity_id, now=now)
    base_inputs = {
        "entity_id": entity_id,
        "policy_version": PLANNER_POLICY_VERSION,
        "decisions_taken": int(decisions_taken),
        "max_decisions": int(max_decisions),
        "session_history": history,
        "now": now,
    }

    def decide(action, stop, reason, target, details, assessment_id):
        payload = dict(base_inputs)
        payload.update({
            "assessment_id": assessment_id,
            "action": action, "stop_reason": stop,
            "reason_code": reason, "target_domain": target,
        })
        merged = dict(details)
        merged.update({
            "decided_at": now, "assessment_id": assessment_id,
            "entity_id": entity_id, "planner_inputs": {
                "decisions_taken": int(decisions_taken),
                "max_decisions": int(max_decisions),
                "session_history": history,
            },
        })
        return PlannerDecision(
            action=action, stop_reason=stop, reason_code=reason,
            target_domain=target, decision_id=_decision_hash(payload),
            details=merged,
        )

    assessment = persisted_assessment(conn, entity_id)
    if assessment is None:
        return decide(None, STOP_NO_ASSESSMENT, "no_sealed_assessment",
                      None, {}, "none")

    assessment_id = str(assessment["id"])
    summary = assessment.get("summary") or {}
    blocking = tuple(sorted(
        str(d) for d in summary.get("blocking_mandatory_domains", ())
    ))

    if assessment["analysis_ready"] and not summary.get("integrity_issue_count"):
        return decide(None, STOP_SUFFICIENT, "all_mandatory_domains_ready",
                      None, {"facts_as_of": assessment["facts_as_of"]},
                      assessment_id)
    if assessment["analysis_ready"] and summary.get("integrity_issue_count"):
        # An analysis_ready flag sealed before reader-detected integrity
        # issues must not fail open into sufficient_state.
        return decide(None, STOP_INTEGRITY, "assessment_reports_integrity_issues",
                      None, {"integrity_issue_count":
                             summary.get("integrity_issue_count")},
                      assessment_id)

    if decisions_taken >= max_decisions:
        return decide(None, STOP_POLICY, "max_decisions_reached", None,
                      {"max_decisions": max_decisions}, assessment_id)

    if history["blocked_failed_streak_in_cooldown_horizon"] >= _COOLDOWN_SESSIONS:
        return decide(None, STOP_COOLDOWN,
                      "consecutive_blocked_or_failed_sessions", None,
                      {"streak": history["blocked_failed_streak_in_cooldown_horizon"],
                       "ceiling": _COOLDOWN_SESSIONS,
                       "horizon_seconds": COOLDOWN_SECONDS}, assessment_id)

    if history["partial_sessions_in_retry_window"] >= _RETRY_CEILING:
        return decide(None, STOP_RETRIES, "partial_acquisition_retry_ceiling",
                      None, {"retries": history["partial_sessions_in_retry_window"],
                             "ceiling": _RETRY_CEILING,
                             "window_seconds": RETRY_WINDOW_SECONDS}, assessment_id)

    domains = {str(item["domain"]): str(item["state"])
               for item in assessment.get("domains", ())}
    blocking_current = [d for d in blocking if domains.get(d) not in READY_STATES]

    action_spec = ACTIONS["acquire_official_website"]
    improvable = sorted(d for d in blocking_current
                        if d in action_spec["improves_domains"])
    unsupported = sorted(d for d in blocking_current
                         if d in UNSUPPORTED_DOMAINS)

    if improvable:
        target = improvable[0]
        return decide("acquire_official_website", None,
                      "deficient_domain_supported_by_collector", target,
                      {"collector": action_spec["collector"],
                       "blocking": sorted(blocking_current)}, assessment_id)

    return decide(None, STOP_UNSUPPORTED, "no_allowlisted_action_for_deficiency",
                  unsupported[0] if unsupported else None,
                  {"blocking": sorted(blocking_current)}, assessment_id)
