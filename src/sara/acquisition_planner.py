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

PLANNER_POLICY_VERSION = "acquisition-planner-v3"

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
STOP_STALE = "stale_assessment"
STOP_IN_FLIGHT = "acquisition_in_progress"
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


def _entity_lineage(conn: sqlite3.Connection, entity_id: str) -> list[str]:
    """Every business_entity subject that redirects into entity_id.

    Sessions are immutable and keep the target subject they were run
    against, so after identity convergence the canonical entity must see
    its merged predecessors' history; otherwise retry/cooldown state
    silently disappears.
    """
    rows = conn.execute(
        "WITH RECURSIVE lineage(id) AS ("
        "SELECT ? "
        "UNION "
        "SELECT ks.id FROM knowledge_subjects ks "
        "JOIN lineage l ON ks.merged_into_subject_id=l.id "
        "WHERE ks.kind='business_entity' AND ks.record_state='merged'"
        ") SELECT id FROM lineage ORDER BY id",
        (entity_id,),
    ).fetchall()
    return [str(row[0]) for row in rows]


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
    lineage = _entity_lineage(conn, entity_id)
    lineage_marks = ",".join("?" for _ in lineage)
    rows = conn.execute(
        f"SELECT status, started_at, target_subject_id FROM acquisition_sessions "
        f"WHERE target_subject_id IN ({lineage_marks}) "
        f"AND collector_name='sara.website'",
        tuple(lineage),
    ).fetchall()
    # started_at is plain TEXT: lexical SQL ordering is wrong across
    # differing UTC offsets. Parse every timestamp first, sort by the
    # normalized instant (newest first), and only then derive the
    # consecutive streak and window counts. An unparseable timestamp
    # fails closed by keeping that session pinned at the newest edge.
    parsed: list[tuple[datetime, str, str]] = []
    corrupt: list[str] = []  # statuses of sessions with unparseable timestamps
    for status, started_at, _target in rows:
        try:
            when = _instant(str(started_at))
        except ValueError:
            corrupt.append(str(status))  # fail closed, count preserved
            continue
        parsed.append((when, status, str(started_at)))
    parsed.sort(key=lambda item: item[0], reverse=True)
    # Every corrupt-timestamp session is pinned at the newest edge with
    # its own status: two corrupt partials hit the retry ceiling, three
    # corrupt blocked/failed sessions hold cooldown.
    corrupt_failures = sum(1 for s in corrupt if s in ("blocked", "failed"))
    corrupt_partials = sum(1 for s in corrupt if s == "partial")

    streak = corrupt_failures
    for _when, status, _raw in parsed:
        if status in ("blocked", "failed"):
            streak += 1
        else:
            break
    streak_recent = corrupt_failures
    for when, status, _raw in parsed:
        if status not in ("blocked", "failed"):
            break
        if when >= cooldown_floor:
            streak_recent += 1
        else:
            break
    partials_in_window = corrupt_partials
    for when, status, _raw in parsed:
        if status != "partial":
            continue
        if when >= retry_floor:
            partials_in_window += 1
    in_flight = int(conn.execute(
        f"SELECT COUNT(*) FROM acquisition_sessions "
        f"WHERE target_subject_id IN ({lineage_marks}) "
        f"AND collector_name='sara.website' AND status IN ('planned','running')",
        tuple(lineage),
    ).fetchone()[0])
    latest_terminal = conn.execute(
        f"SELECT started_at, finished_at FROM acquisition_sessions "
        f"WHERE target_subject_id IN ({lineage_marks}) "
        f"AND collector_name='sara.website' "
        f"AND status IN ('complete','partial') "
        f"ORDER BY started_at DESC LIMIT 1",
        tuple(lineage),
    ).fetchone()
    return {
        "session_count": len(rows),
        "entity_lineage": lineage,
        "in_flight_sessions": in_flight,
        "latest_terminal_session_started_at":
            str(latest_terminal[0]) if latest_terminal else None,
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
    # decisions_taken is deliberately NOT sealed into the identity: the
    # counter includes previously persisted action decisions, so sealing
    # it would make every retry mint a new id and break replay. It stays
    # in the persisted details snapshot for observability, and the
    # ceiling still enforces it at decision time.
    base_inputs = {
        "entity_id": entity_id,
        "policy_version": PLANNER_POLICY_VERSION,
        "max_decisions": int(max_decisions),
        "session_history": history,
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

    if history["in_flight_sessions"] > 0:
        return decide(None, STOP_IN_FLIGHT, "acquisition_session_active",
                      None, {"in_flight": history["in_flight_sessions"]},
                      assessment_id)

    latest_started = history.get("latest_terminal_session_started_at")
    if latest_started:
        try:
            latest_dt = _instant(latest_started)
        except ValueError:
            latest_dt = None  # corrupt timestamp: no stale signal; the
            # session is already counted fail-closed in the history counts
        if latest_dt is not None:
            try:
                as_of_dt = _instant(str(assessment["facts_as_of"]))
            except ValueError:
                return decide(None, STOP_STALE,
                              "assessment_currency_unprovable_corrupt_timestamp",
                              None, {"latest_session_started_at": latest_started},
                              assessment_id)
            # The assessment must cover the latest evidence-producing
            # acquisition; a complete/partial session that started after
            # the assessment's facts watermark is unassessed state.
            if latest_dt > as_of_dt:
                return decide(None, STOP_STALE,
                              "assessment_older_than_latest_acquisition",
                              None, {"assessment_facts_as_of":
                                     str(assessment["facts_as_of"]),
                                     "latest_session_started_at": latest_started},
                              assessment_id)
    summary = assessment.get("summary") or {}
    blocking = tuple(sorted(
        str(d) for d in summary.get("blocking_mandatory_domains", ())
    ))

    # Reader-detected integrity issues are the authority: the reader
    # recomputes chronology/consistency facts (facts_as_of vs computed_at,
    # seal ordering, ready-flag coherence) against the persisted rows and
    # catches internally inconsistent or directly populated historical
    # assessments whose sealed summary count says zero. The sealed count
    # remains a secondary signal only.
    reader_issues = list(assessment.get("integrity_issues") or [])
    sealed_count = int(summary.get("integrity_issue_count") or 0)
    if reader_issues or sealed_count:
        return decide(None, STOP_INTEGRITY, "assessment_reports_integrity_issues",
                      None, {"reader_integrity_codes":
                             sorted({str(item.get("code")) for item in reader_issues}),
                             "sealed_integrity_issue_count": sealed_count},
                      assessment_id)

    if assessment["analysis_ready"]:
        return decide(None, STOP_SUFFICIENT, "all_mandatory_domains_ready",
                      None, {"facts_as_of": assessment["facts_as_of"]},
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
