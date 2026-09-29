"""Deterministic dossier-driven acquisition planning.

The planner reads the persisted dossier assessment for one entity and
decides the single next bounded acquisition action, or stops. It never
mutates facts, observations, or evidence: planning is a pure decision
over assessment state, and the only persisted output is the decision
record itself.

Invariants:
- same dossier assessment + same planner policy/version -> same decision;
- every decision cycle ends in exactly one allowlisted action or one
  stop, never both and never neither;
- the decision graph is acyclic by construction: each stop reason is
  terminal and each action names the domain it intends to improve, so a
  cycle would require a domain to regress, which reassessment alone
  cannot cause without an intervening acquisition.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

PLANNER_POLICY_VERSION = "acquisition-planner-v1"

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


def _decision_hash(entity_id: str, assessment_id: str, action: str | None,
                   stop_reason: str | None, reason_code: str, target_domain: str | None) -> str:
    payload = json.dumps(
        [entity_id, assessment_id, action, stop_reason, reason_code, target_domain,
         PLANNER_POLICY_VERSION],
        sort_keys=True, separators=(",", ":"),
    )
    return "plan_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def _blocked_recently(conn: sqlite3.Connection, entity_id: str, *, now: str) -> int:
    """Count the most recent consecutive blocked/failed website sessions."""
    rows = conn.execute(
        "SELECT status FROM acquisition_sessions "
        "WHERE target_subject_id=? AND collector_name='sara.website' "
        "ORDER BY started_at DESC",
        (entity_id,),
    ).fetchall()
    streak = 0
    for row in rows:
        if row[0] in ("blocked", "failed"):
            streak += 1
        else:
            break
    return streak


def _website_retry_count(conn: sqlite3.Connection, entity_id: str, *, since: str) -> int:
    """Partial website sessions since the given timestamp."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM acquisition_sessions "
        "WHERE target_subject_id=? AND collector_name='sara.website' "
        "AND status='partial' AND started_at > ?",
        (entity_id, since),
    ).fetchone()[0])


def plan_next_acquisition(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    now: str,
    decisions_taken: int = 0,
    max_decisions: int = 25,
    since: str = "",
) -> PlannerDecision:
    """Decide the single next action for one entity from its persisted assessment.

    ``now`` is the decision clock (ISO-8601, timezone-aware). ``since``
    bounds the retry window; sessions started before it do not count
    toward the retry ceiling.
    """
    from .dossier.status import persisted_assessment

    assessment = persisted_assessment(conn, entity_id)
    if assessment is None:
        decision = PlannerDecision(
            action=None, stop_reason=STOP_NO_ASSESSMENT,
            reason_code="no_sealed_assessment", target_domain=None,
        )
        return _finalize(decision, entity_id, now)

    if assessment["analysis_ready"]:
        decision = PlannerDecision(
            action=None, stop_reason=STOP_SUFFICIENT,
            reason_code="all_mandatory_domains_ready", target_domain=None,
            details={"facts_as_of": assessment["facts_as_of"]},
        )
        return _finalize(decision, entity_id, now, assessment)

    if decisions_taken >= max_decisions:
        decision = PlannerDecision(
            action=None, stop_reason=STOP_POLICY,
            reason_code="max_decisions_reached",
            target_domain=None,
            details={"max_decisions": max_decisions},
        )
        return _finalize(decision, entity_id, now, assessment)

    blocked_streak = _blocked_recently(conn, entity_id, now=now)
    if blocked_streak >= _COOLDOWN_SESSIONS:
        decision = PlannerDecision(
            action=None, stop_reason=STOP_COOLDOWN,
            reason_code="consecutive_blocked_or_failed_sessions",
            target_domain=None,
            details={"streak": blocked_streak, "ceiling": _COOLDOWN_SESSIONS},
        )
        return _finalize(decision, entity_id, now, assessment)

    retry_window = since or "0001-01-01T00:00:00+00:00"
    retries = _website_retry_count(conn, entity_id, since=retry_window)
    if retries >= _RETRY_CEILING:
        decision = PlannerDecision(
            action=None, stop_reason=STOP_RETRIES,
            reason_code="partial_acquisition_retry_ceiling",
            target_domain=None,
            details={"retries": retries, "ceiling": _RETRY_CEILING},
        )
        return _finalize(decision, entity_id, now, assessment)

    domains = {item["domain"]: item["state"] for item in assessment["domains"]}
    blocking = [
        d for d in assessment.get("blocking_mandatory_domains", ())
        if domains.get(d) not in READY_STATES
    ] or sorted(
        d for d, state in domains.items() if state not in READY_STATES
    )

    action_spec = ACTIONS["acquire_official_website"]
    improvable = [d for d in blocking if d in action_spec["improves_domains"]]
    unsupported = [d for d in blocking if d in UNSUPPORTED_DOMAINS]

    if improvable:
        target = sorted(improvable)[0]
        decision = PlannerDecision(
            action="acquire_official_website", stop_reason=None,
            reason_code="deficient_domain_supported_by_collector",
            target_domain=target,
            details={"collector": action_spec["collector"],
                     "blocking": sorted(blocking)},
        )
        return _finalize(decision, entity_id, now, assessment)

    decision = PlannerDecision(
        action=None, stop_reason=STOP_UNSUPPORTED,
        reason_code="no_allowlisted_action_for_deficiency",
        target_domain=sorted(unsupported)[0] if unsupported else None,
        details={"blocking": sorted(blocking)},
    )
    return _finalize(decision, entity_id, now, assessment)


def _finalize(
    decision: PlannerDecision,
    entity_id: str,
    now: str,
    assessment: dict[str, Any] | None = None,
) -> PlannerDecision:
    assessment_id = (assessment or {}).get("id", "none")
    digest = _decision_hash(
        entity_id, assessment_id, decision.action, decision.stop_reason,
        decision.reason_code, decision.target_domain,
    )
    details = dict(decision.details)
    details["decided_at"] = now
    details["assessment_id"] = assessment_id
    return PlannerDecision(
        action=decision.action, stop_reason=decision.stop_reason,
        reason_code=decision.reason_code, target_domain=decision.target_domain,
        decision_id=digest, details=details,
    )
