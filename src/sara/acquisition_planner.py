"""Deterministic dossier-driven acquisition planning.

The planner reads the persisted dossier assessment for one entity and
decides the single next bounded acquisition action, or stops. It never
mutates facts, observations, or evidence: planning is a pure decision
over assessment state, and the only persisted output is the decision
record itself.

Determinism contract (v8): for the SAME entity, the SAME sealed
assessment, the SAME session-history snapshot, the SAME
Understanding-state fingerprint, and the SAME decision ceiling, the
same policy version yields the same decision id and the same chosen
action or stop. The session-history snapshot (which embeds
the window/horizon counts and lineage) and max_decisions are sealed into
the id; the raw decision clock is deliberately NOT sealed — its effects
enter only through the snapshot counts — and decisions_taken is
deliberately NOT sealed because the counter includes previously
persisted action decisions, so sealing it would make every retry mint a
new id. A lost-output retry therefore reaches the duplicate-key replay
path. Different sealed inputs produce visibly different decisions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# v6: currentness became assessment-signature equality over the
# resolved Entity+Location graph (v5 was Entity-only max-timestamp).
# v7: multi-collector scheduling. extract_retained_reviews addresses
# reputation from retained Maps evidence, and the session-history,
# in-flight, and terminal-watermark queries became collector-agnostic
# over the entity lineage.
# v8: the lineage additionally covers Location subjects (review
# sessions target Locations, not Entities); retry/cooldown ceilings
# are scoped to the candidate action's collector; and
# extract_retained_reviews is schedulable only while a Maps snapshot
# newer than the last complete review extraction remains unmined.
PLANNER_POLICY_VERSION = "acquisition-planner-v8"

#: Domain states that satisfy the assessment's readiness bar.
READY_STATES = frozenset({"sufficient", "strong", "not_applicable"})

#: Allowlisted acquisition actions. Each maps a collector to the
# Understanding domains its evidence can improve.
ACTIONS = {
    "acquire_official_website": {
        "collector": "sara.website",
        "improves_domains": frozenset({
            "digital_presence", "digital_capabilities", "communication",
            "offerings", "business_model",
        }),
        # v8: session statuses that count toward this collector's
        # scoped retry ceiling.
        "retry_statuses": frozenset({"partial"}),
        "scope": "business",
    },
    # v7: retained-review extraction is local (no network) — it turns
    # reviews already retained in the Maps snapshot raw evidence into
    # observations, which is what reputation assessments consume.
    "extract_retained_reviews": {
        "collector": "sara.reviews.maps_snapshot",
        "improves_domains": frozenset({"reputation"}),
        # v8 (F-06): review extraction is all-or-nothing and local, so
        # its deterministic failed attempts count toward its own retry
        # ceiling alongside (never occurring) partials.
        "retry_statuses": frozenset({"partial", "failed"}),
        "scope": "entity",
    },
}

#: Deficiencies the allowlisted actions cannot address. Deciding on one
#: of these stops the cycle rather than mapping it to an unsupported
#: collector.
UNSUPPORTED_DOMAINS = frozenset({
    "competitive_context", "customer_journey", "customer_market",
    "identity", "locations", "classification",
    "marketing", "technology", "people", "operations", "change",
    "scale", "unknowns", "provenance",
})

STOP_SUFFICIENT = "sufficient_state"
STOP_STALE = "stale_assessment"
STOP_STALE_UNDERSTANDING = "stale_understanding_state"
STOP_IN_FLIGHT = "acquisition_in_progress"
STOP_STALE_IN_FLIGHT = "stale_in_flight_session"
#: A running/planned session older than this is an orphan from a
#: dead process: planning stops and demands recovery rather than
#: blocking forever or silently scheduling over it (6 hours).
IN_FLIGHT_HORIZON_SECONDS = 6 * 3600

#: Permitted clock skew between the decision clock and a session row.
#: A started_at further in the future than this allowance is invalid
#: chronology (misconfigured clock or tampered row) and routes to the
#: recovery-required stop instead of staying "fresh" (5 minutes).
IN_FLIGHT_SKEW_SECONDS = 5 * 60
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
    """Parse a persisted ISO timestamp to a UTC instant.

    Every failure mode — naive values, malformed text, and offsets whose
    UTC normalization leaves Python's datetime range — raises ValueError
    so callers fail closed uniformly.
    """
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError(f"timestamp {value!r} is not timezone-aware")
        return parsed.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError(f"timestamp {value!r} normalizes outside the "
                         f"supported datetime range") from exc


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


def _lineage_subjects(conn: sqlite3.Connection, entity_id: str) -> dict[str, list[str]]:
    """Every subject the entity's acquisition history lives on.

    v8 (F-02): review extraction sessions target their frozen
    SOURCE-TIME Location subject, not the Business Entity. The
    lineage therefore covers the reverse Business-Entity merge
    closure AND every location owned by an entity in that closure;
    otherwise review sessions would be invisible to in-flight
    suppression, terminal chronology, and retry state.
    """
    entities = _entity_lineage(conn, entity_id)
    entity_marks = ",".join("?" for _ in entities)
    location_rows = conn.execute(
        f"SELECT bl.id FROM business_locations bl "
        f"WHERE bl.business_entity_id IN ({entity_marks}) ORDER BY bl.id",
        tuple(entities),
    ).fetchall()
    return {
        "entities": entities,
        "locations": [str(row[0]) for row in location_rows],
    }


def _review_collector_name() -> str:
    from .reviews.model import COLLECTOR_NAME
    return COLLECTOR_NAME


def _session_history(conn: sqlite3.Connection, entity_id: str, *, now: str) -> dict[str, Any]:
    """Snapshot the acquisition-session facts the decision depends on.

    Timestamps are compared as normalized UTC instants, never lexically.
    v8 (F-03): retry-window and cooldown ceilings are derived PER
    COLLECTOR — only sessions of a candidate action's own collector
    over the lineage count, so heterogeneous collectors never inherit
    each other's failure state. In-flight suppression and the terminal
    evidence watermark stay entity-wide across collectors: any active
    session holds planning, and any evidence-producing session finishing
    after the assessment seal leaves unassessed evidence behind.
    Terminal (complete/partial) freshness is judged on finished_at and
    fails closed to stale when a finish timestamp is corrupt or missing;
    future-dated in-flight rows are invalid chronology and demand
    recovery rather than counting as fresh.
    """
    now_dt = _instant(now)
    retry_floor = datetime.fromtimestamp(
        now_dt.timestamp() - RETRY_WINDOW_SECONDS, tz=timezone.utc
    )
    cooldown_floor = datetime.fromtimestamp(
        now_dt.timestamp() - COOLDOWN_SECONDS, tz=timezone.utc
    )
    lineage = _lineage_subjects(conn, entity_id)
    subjects = lineage["entities"] + lineage["locations"]
    lineage_marks = ",".join("?" for _ in subjects)
    rows = conn.execute(
        f"SELECT status, started_at, target_subject_id, collector_name "
        f"FROM acquisition_sessions "
        f"WHERE target_subject_id IN ({lineage_marks})",
        tuple(subjects),
    ).fetchall()
    # started_at is plain TEXT: lexical SQL ordering is wrong across
    # differing UTC offsets. Parse every timestamp first, sort by the
    # normalized instant (newest first), and only then derive the
    # consecutive streak and window counts. An unparseable timestamp
    # fails closed by keeping that session pinned at the newest edge.
    parsed: list[tuple[datetime, str, str]] = []
    corrupt: list[tuple[str, str]] = []  # (status, collector) unparseable
    for status, started_at, _target, collector in rows:
        try:
            when = _instant(str(started_at))
        except ValueError:
            corrupt.append((str(status), str(collector)))  # fail closed
            continue
        parsed.append((when, str(status), str(collector)))
    parsed.sort(key=lambda item: item[0], reverse=True)

    # v8 per-collector ceilings: every corrupt-timestamp session is
    # pinned at the newest edge of ITS OWN collector's history with
    # its own status (fail closed, count preserved).
    retry_statuses: dict[str, set[str]] = {}
    for spec in ACTIONS.values():
        collector = str(spec["collector"])
        retry_statuses.setdefault(collector, set()).update(
            spec.get("retry_statuses", ())
        )
    collector_histories: dict[str, dict[str, Any]] = {}
    for collector in sorted(retry_statuses):
        statuses = retry_statuses[collector]
        mine = [item for item in parsed if item[2] == collector]
        my_corrupt = [s for s, c in corrupt if c == collector]
        streak = sum(1 for s in my_corrupt if s in ("blocked", "failed"))
        for _when, status, _c in mine:
            if status in ("blocked", "failed"):
                streak += 1
            else:
                break
        streak_recent = sum(1 for s in my_corrupt if s in ("blocked", "failed"))
        for when, status, _c in mine:
            if status not in ("blocked", "failed"):
                break
            if when >= cooldown_floor:
                streak_recent += 1
            else:
                break
        retries = sum(1 for s in my_corrupt if s in statuses)
        for when, status, _c in mine:
            if status in statuses and when >= retry_floor:
                retries += 1
        collector_histories[collector] = {
            "blocked_failed_streak_total": streak,
            "blocked_failed_streak_in_cooldown_horizon": streak_recent,
            "retry_sessions_in_window": retries,
            "retry_statuses": sorted(statuses),
        }
    in_flight_rows = conn.execute(
        # Any active session on the lineage holds planning, regardless
        # of collector — the planner schedules at most one acquisition
        # per entity at a time.
        f"SELECT started_at FROM acquisition_sessions "
        f"WHERE target_subject_id IN ({lineage_marks}) "
        f"AND status IN ('planned','running')",
        tuple(subjects),
    ).fetchall()
    in_flight = len(in_flight_rows)
    in_flight_orphaned = 0
    in_flight_fresh = 0
    in_flight_corrupt = 0
    for (started_at,) in in_flight_rows:
        try:
            when = _instant(str(started_at))
        except ValueError:
            in_flight_corrupt += 1  # age unknowable: treat as orphan
            in_flight_orphaned += 1
            continue
        age = (now_dt - when).total_seconds()
        if age < -IN_FLIGHT_SKEW_SECONDS:
            # started_at is materially in the future: invalid chronology,
            # never "fresh" (which would deadlock until that date).
            in_flight_corrupt += 1
            in_flight_orphaned += 1
        elif age <= IN_FLIGHT_HORIZON_SECONDS:
            in_flight_fresh += 1
        else:
            in_flight_orphaned += 1
    # S-01: choose the terminal evidence-producing session by parsed UTC
    # chronology over finished_at (the watermark includes acquisition
    # finish times), never by lexical TEXT ordering and never started_at
    # alone — a session that started before an assessment but finished
    # after it leaves unassessed evidence behind.
    terminal_rows = conn.execute(
        # Evidence currency is per-entity, not per-collector: a
        # review extraction finishing after the seal leaves just as
        # much unassessed evidence behind as a website crawl does.
        f"SELECT started_at, finished_at, collector_name "
        f"FROM acquisition_sessions "
        f"WHERE target_subject_id IN ({lineage_marks}) "
        f"AND status IN ('complete','partial')",
        tuple(subjects),
    ).fetchall()
    review_collector = _review_collector_name()
    newest_terminal: tuple[datetime, str] | None = None
    corrupt_terminal = 0
    unprovable_terminal = False
    newest_review_terminal: tuple[datetime, str] | None = None
    review_unprovable = False
    for started_at, finished_at, collector in terminal_rows:
        # Freshness is defined on terminal completion: finished_at. A
        # corrupt or missing finish timestamp makes completion chronology
        # unprovable and fails closed to stale; started_at is never a
        # substitute for a complete/partial row's finish time.
        try:
            when = _instant(str(finished_at))
        except (ValueError, TypeError):
            corrupt_terminal += 1
            unprovable_terminal = True
            if str(collector) == review_collector:
                review_unprovable = True
            continue
        if newest_terminal is None or when > newest_terminal[0]:
            newest_terminal = (when, str(finished_at))
        if str(collector) == review_collector and (
            newest_review_terminal is None or when > newest_review_terminal[0]
        ):
            newest_review_terminal = (when, str(finished_at))
    if unprovable_terminal:
        # At least one terminal row's completion time cannot be proven:
        # refuse to certify assessment currency.
        latest_terminal = None
        stale_unprovable = True
    else:
        latest_terminal = (
            (newest_terminal[1],) if newest_terminal is not None else None
        )
        stale_unprovable = False
    return {
        "session_count": len(rows),
        "lineage_subjects": lineage,
        "in_flight_sessions": in_flight,
        "in_flight_fresh": in_flight_fresh,
        "in_flight_orphaned": in_flight_orphaned,
        "in_flight_corrupt": in_flight_corrupt,
        "in_flight_horizon_seconds": IN_FLIGHT_HORIZON_SECONDS,
        "latest_terminal_session_time":
            str(latest_terminal[0]) if latest_terminal else None,
        "corrupt_terminal_timestamps": corrupt_terminal,
        "terminal_chronology_unprovable": stale_unprovable,
        "latest_review_terminal_time": (
            str(newest_review_terminal[1]) if newest_review_terminal else None
        ),
        "review_terminal_chronology_unprovable": review_unprovable,
        "collector_histories": collector_histories,
        "retry_window_seconds": RETRY_WINDOW_SECONDS,
        "cooldown_horizon_seconds": COOLDOWN_SECONDS,
    }


def _review_extraction_pending(
    conn: sqlite3.Connection, history: dict[str, Any]
) -> bool | None:
    """Whether a retained Maps snapshot still needs review extraction.

    True  — at least one Maps-linked business snapshot is newer than
            the newest complete review extraction over the lineage, or
            snapshots exist and none was ever extracted.
    False — every retained snapshot is already mined.
    None  — chronology is unprovable (corrupt timestamps): fail
            closed, the action is not scheduled.

    This is the F-01 termination guard: after an entity-scoped
    extraction mines every current snapshot, re-scheduling the same
    action would be a provable no-op. A newer Maps sync re-arms it.
    """
    if history.get("review_terminal_chronology_unprovable"):
        return None
    finish_raw = history.get("latest_review_terminal_time")
    review_finish = _instant(str(finish_raw)) if finish_raw else None
    locations = history["lineage_subjects"]["locations"]
    if not locations:
        return False
    marks = ",".join("?" for _ in locations)
    rows = conn.execute(
        f"SELECT b.last_seen_at FROM maps_business_location_links m "
        f"JOIN businesses b ON b.id=m.business_id "
        f"WHERE m.location_id IN ({marks})",
        tuple(locations),
    ).fetchall()
    newest_snapshot: datetime | None = None
    for (last_seen_at,) in rows:
        try:
            when = _instant(str(last_seen_at))
        except ValueError:
            return None  # corrupt snapshot chronology: fail closed
        if newest_snapshot is None or when > newest_snapshot:
            newest_snapshot = when
    if newest_snapshot is None:
        return False  # no Maps-linked business: nothing retained to mine
    if review_finish is None:
        return True
    return newest_snapshot > review_finish


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
    ``now``. The decision id seals the entity, assessment, session-history
    snapshot, understanding-state fingerprint, decision ceiling, outputs,
    and policy version; the raw clock and decisions_taken are
    deliberately excluded (see the module docstring).
    """
    from .dossier.status import persisted_assessment

    history = _session_history(conn, entity_id, now=now)
    # Source-agnostic currency via the assessment identity contract: the
    # fingerprint is the same input signature the assessment sealed, so
    # equality proves the assessment still represents current state.
    from .dossier.assessment import understanding_state_fingerprint
    state_fingerprint = understanding_state_fingerprint(
        conn, entity_id=entity_id, evaluated_at=now)
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
        "understanding_state_fingerprint": state_fingerprint,
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
                "understanding_state_fingerprint": state_fingerprint,
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

    # Reader/sealed integrity is evaluated FIRST, immediately after
    # assessment retrieval and before any acquisition-state or currency
    # evaluation: an internally inconsistent assessment never yields an
    # in-flight, stale, or sufficient decision.
    reader_issues = list(assessment.get("integrity_issues") or [])
    sealed_count = int(summary.get("integrity_issue_count") or 0)
    if reader_issues or sealed_count:
        return decide(None, STOP_INTEGRITY, "assessment_reports_integrity_issues",
                      None, {"reader_integrity_codes":
                             sorted({str(item.get("code")) for item in reader_issues}),
                             "sealed_integrity_issue_count": sealed_count},
                      assessment_id)

    if history["in_flight_sessions"] > 0:
        if history["in_flight_fresh"] > 0:
            return decide(None, STOP_IN_FLIGHT, "acquisition_session_active",
                          None, {"in_flight": history["in_flight_sessions"],
                                 "fresh": history["in_flight_fresh"],
                                 "orphaned": history["in_flight_orphaned"]},
                          assessment_id)
        # Every in-flight session is older than the horizon (or of
        # unknowable age): a dead process left it behind. Planning stops
        # and demands recovery; it neither blocks forever nor schedules
        # over the orphan.
        return decide(None, STOP_STALE_IN_FLIGHT, "orphaned_acquisition_session",
                      None, {"in_flight": history["in_flight_sessions"],
                             "orphaned": history["in_flight_orphaned"],
                             "corrupt": history.get("in_flight_corrupt", 0),
                             "horizon_seconds": IN_FLIGHT_HORIZON_SECONDS},
                      assessment_id)

    # Source-agnostic currency: the assessment's facts watermark must
    # cover the newest durable Understanding state across the lineage —
    # facts, observations, evidence, subjects, Maps links, identifiers —
    # regardless of which collector or sync produced it. A later Maps
    # synchronization therefore forces a refresh before planning.
    try:
        as_of_dt = _instant(str(assessment["facts_as_of"]))
    except ValueError:
        return decide(None, STOP_STALE,
                      "assessment_currency_unprovable_corrupt_timestamp",
                      None, {"facts_as_of": str(assessment["facts_as_of"])},
                      assessment_id)
    if state_fingerprint.get("unprovable"):
        # A corrupt relevant state timestamp makes current state
        # unprovable: fail closed rather than ignoring it.
        return decide(None, STOP_STALE,
                      "understanding_state_chronology_unprovable",
                      None, {"fingerprint_error":
                             state_fingerprint.get("error")},
                      assessment_id)
    sealed_signature = str(summary.get("input_signature_sha256") or "")
    current_signature = str(
        state_fingerprint.get("input_signature_sha256") or "")
    if sealed_signature != current_signature:
        # The sealed input signature is the assessment identity contract:
        # any divergence — Maps-side facts, Location mutations, identifier
        # changes, freshness transitions, support-graph changes — means
        # the persisted assessment no longer represents current state.
        return decide(None, STOP_STALE_UNDERSTANDING,
                      "understanding_state_signature_diverged",
                      None, {"assessment_facts_as_of":
                             str(assessment["facts_as_of"]),
                             "sealed_input_signature_sha256": sealed_signature,
                             "current_input_signature_sha256": current_signature},
                      assessment_id)

    if history.get("terminal_chronology_unprovable"):
        return decide(None, STOP_STALE,
                      "assessment_currency_unprovable_corrupt_timestamp",
                      None, {"corrupt_terminal_timestamps":
                             history.get("corrupt_terminal_timestamps", 0)},
                      assessment_id)
    latest_terminal_time = history.get("latest_terminal_session_time")
    if latest_terminal_time:
        latest_dt = _instant(latest_terminal_time)  # pre-parsed upstream
        # The assessment must cover the newest terminal evidence-producing
        # acquisition by its finish time; a session that finished after
        # the assessment's facts watermark left unassessed evidence.
        if latest_dt > as_of_dt:
            return decide(None, STOP_STALE,
                          "assessment_older_than_latest_acquisition",
                          None, {"assessment_facts_as_of":
                                 str(assessment["facts_as_of"]),
                                 "latest_terminal_session_time": latest_terminal_time},
                          assessment_id)
    blocking = tuple(sorted(
        str(d) for d in summary.get("blocking_mandatory_domains", ())
    ))

    if assessment["analysis_ready"]:
        return decide(None, STOP_SUFFICIENT, "all_mandatory_domains_ready",
                      None, {"facts_as_of": assessment["facts_as_of"]},
                      assessment_id)

    if decisions_taken >= max_decisions:
        return decide(None, STOP_POLICY, "max_decisions_reached", None,
                      {"max_decisions": max_decisions}, assessment_id)

    domains = {str(item["domain"]): str(item["state"])
               for item in assessment.get("domains", ())}
    blocking_current = [d for d in blocking if domains.get(d) not in READY_STATES]

    improvable = sorted(
        d for d in blocking_current
        if any(d in spec["improves_domains"] for spec in ACTIONS.values()))
    unsupported = sorted(d for d in blocking_current
                         if d in UNSUPPORTED_DOMAINS)

    # v8 candidate-scoped selection (F-03/F-04): the lexically first
    # improvable domain is the target, and among the actions improving
    # it the lexically first name wins — but an action is only
    # eligible when ITS OWN collector is not at a retry/cooldown
    # ceiling, and the review action additionally requires an unmined
    # retained snapshot. The schedule stays deterministic and
    # replay-stable.
    ceiling_stops: list[tuple[str, str, str, str, dict[str, Any]]] = []
    actions_not_applicable: list[str] = []
    for domain in improvable:
        for action_name in sorted(
            name for name, spec in ACTIONS.items()
            if domain in spec["improves_domains"]
        ):
            spec = ACTIONS[action_name]
            collector = str(spec["collector"])
            collector_state = history["collector_histories"][collector]
            if (
                collector_state["blocked_failed_streak_in_cooldown_horizon"]
                >= _COOLDOWN_SESSIONS
            ):
                ceiling_stops.append((
                    domain, action_name, STOP_COOLDOWN,
                    "consecutive_blocked_or_failed_sessions",
                    {"collector": collector,
                     "streak": collector_state[
                         "blocked_failed_streak_in_cooldown_horizon"],
                     "ceiling": _COOLDOWN_SESSIONS,
                     "horizon_seconds": COOLDOWN_SECONDS},
                ))
                continue
            if collector_state["retry_sessions_in_window"] >= _RETRY_CEILING:
                ceiling_stops.append((
                    domain, action_name, STOP_RETRIES,
                    "partial_acquisition_retry_ceiling",
                    {"collector": collector,
                     "retries": collector_state["retry_sessions_in_window"],
                     "ceiling": _RETRY_CEILING,
                     "window_seconds": RETRY_WINDOW_SECONDS},
                ))
                continue
            if action_name == "extract_retained_reviews" and (
                _review_extraction_pending(conn, history) is not True
            ):
                actions_not_applicable.append(action_name)
                continue
            return decide(action_name, None,
                          "deficient_domain_supported_by_collector", domain,
                          {"collector": collector,
                           "scope": str(spec.get("scope", "business")),
                           "blocking": sorted(blocking_current)}, assessment_id)
    if ceiling_stops:
        # No improvable domain has an eligible action, and at least one
        # candidate is ceiling-blocked: report the lexically first
        # improvable domain's first ceiling.
        _domain, action_name, stop, reason, details = ceiling_stops[0]
        return decide(None, stop, reason, None,
                      {**details, "action": action_name}, assessment_id)

    return decide(None, STOP_UNSUPPORTED, "no_allowlisted_action_for_deficiency",
                  unsupported[0] if unsupported else None,
                  {"blocking": sorted(blocking_current),
                   "actions_not_applicable": actions_not_applicable},
                  assessment_id)
