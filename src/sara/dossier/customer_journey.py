from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

#: Local constants rather than imports from the website package: importing
#: sara.website would pull its collector CLI, which imports this package.
_OFFICIAL_WEB_SOURCE_ID = "src_official_web"
_WEBSITE_COLLECTOR_NAME = "sara.website"
_GOOGLE_MAPS_SOURCE_ID = "src_google_maps"

RECONSTRUCTION_VERSION = "observable-customer-journey-v1"

#: The contract's `book/order` stage is represented by the `book_order` key.
STAGE_ORDER = (
    "discover",
    "evaluate",
    "contact",
    "book_order",
    "pay",
    "receive",
    "support",
    "return",
)

_STAGE_LABELS = {
    "discover": "discover",
    "evaluate": "evaluate",
    "contact": "contact",
    "book_order": "book/order",
    "pay": "pay",
    "receive": "receive",
    "support": "support",
    "return": "return",
}

#: Website-evidence currency window for journey METADATA contributions
#: (page roles, channel candidates) — the only journey inputs with no
#: predicate freshness policy of their own. Fact-derived contributions
#: inherit their fact's per-predicate freshness window instead (RCJ-04).
_JOURNEY_EVIDENCE_FRESHNESS_DAYS = 30

#: Session-config keys the website producer has written since the journey
#: contract was defined. Verification requires these keys (canonical
#: bytes, hash, deterministic id); EXTRA keys from later collector
#: versions are tolerated so a version bump does not invalidate durable
#: historical sessions (RCJ-01).
_WEBSITE_SESSION_REQUIRED_CONFIG_KEYS = frozenset({
    "entity_id", "start_url", "page_limit", "depth_limit",
    "max_response_bytes", "timeout_seconds", "request_interval_seconds",
    "max_policy_delay_seconds", "retry_attempt_limit",
    "retry_base_delay_seconds", "retry_max_delay_seconds",
    "retry_delay_budget_seconds", "user_agent", "obey_robots",
    "evidence_root",
})

#: Evidence-metadata keys the producer writes per retained page. Required
#: as above; extras tolerated for forward compatibility (RCJ-01/RCJ-03).
_WEBSITE_EVIDENCE_REQUIRED_METADATA_KEYS = frozenset({
    "acquisition_kind", "entity_id", "start_url", "requested_url",
    "final_url", "crawl_depth", "page_role", "home_page",
    "business_wide_scope_eligible", "crawl_frontier_exhausted",
    "title", "canonical_url", "channels", "observation_ids",
})

#: The website collector version at which frontier-exhaustion semantics
#: became safe for bounded absence claims (v5; pre-v5 depth semantics
#: could report exhaustion with whole surfaces uninspected). Absence
#: support from older sessions must not establish journey bounded
#: not-observed (RCJ-01).
_ABSENCE_SAFE_WEBSITE_VERSION = 5

#: Positive-evidence compatible website collector versions: every version
#: from the absence-safe era onward, whose producer output this reader
#: structurally verifies (config/metadata contracts, deterministic ids,
#: observation seals). OLDER versions are silently out of scope for
#: journey evidence — never integrity failures — and future versions are
#: admitted by the same structural verification (R4). The lower bound
#: and the absence-safe bound are deliberately the same era.
def _positive_evidence_version(version: object) -> bool:
    try:
        parsed = int(str(version))
    except (TypeError, ValueError):
        return False
    return parsed >= _ABSENCE_SAFE_WEBSITE_VERSION

_SOCIAL_CHANNEL_TYPES = {
    "instagram", "facebook", "linkedin", "x", "tiktok", "youtube",
}

_EVALUATE_PAGE_ROLES = {"home", "about", "offerings"}

#: Maps-anchored facts whose usable Maps-source support proves the Maps
#: listing is a live discovery surface.
_MAPS_ANCHOR_PREDICATES = {
    "business.name.trading",
    "location.address",
    "location.latitude",
    "location.longitude",
    "location.phone",
}

_MISSING_KNOWLEDGE = {
    "pay": ["no explicit payment or checkout evidence is retained by any v1 source"],
    "receive": ["no explicit fulfillment, delivery, pickup, or in-person evidence is retained by any v1 source"],
    "return": ["no explicit repeat, renewal, loyalty, subscription, or reorder evidence is retained by any v1 source"],
}


def _parse_instant(value: object, *, field: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _supported_fact(fact: dict[str, Any]) -> bool:
    """A fact Sara currently stands behind, regardless of freshness.

    CJ-03: stale facts remain journey contributions with current=False —
    an aged-out Maps phone fact must mark the contact stage stale, not
    make the stage vanish into unknown/not_started. The explicit
    value-bearing status="stale" the schema and assessment policy
    recognize is included: it is history Sara stands behind, aged out.
    """
    return fact["status"] in {"confirmed", "single_source", "stale"}


def _fresh_fact(fact: dict[str, Any]) -> bool:
    return (
        fact["status"] in {"confirmed", "single_source"}
        and not bool(fact["freshness"]["is_stale"])
    )


def _fact_current(fact: dict[str, Any]) -> bool:
    """A supported fact's currency: within its per-predicate window AND
    not explicitly marked stale by reconciliation."""
    return (
        not bool(fact["freshness"]["is_stale"])
        and fact["status"] != "stale"
    )


def _entity_lineage(conn: sqlite3.Connection, entity_id: str) -> list[str]:
    """The canonical Entity plus every business_entity that merged into it.

    CJ-01: journey evidence admission is scoped to this set. A crawl run
    against a predecessor Entity before convergence is the same business's
    history; another canonical Entity's evidence is simply not ours and
    must neither be used nor flagged.
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


def _bounded_absence_current(
    fact: dict[str, Any], absence_safe_sessions: frozenset[str]
) -> bool:
    """Bounded not-observed for the journey, bound to the producer's own
    absence contract (R2): the value was not observed, the inspection is
    current, and the absence is supported by a session that the journey
    reader has ALREADY producer-verified AND that satisfies the exact
    conditions under which the website collector claims absence —
    status complete, business-wide scope eligible, frontier exhausted,
    website collector at an absence-safe version (>= v5). A complete but
    budget-truncated or scope-ineligible session can never establish
    bounded not-observed. never absence."""
    return (
        fact["status"] == "not_observed"
        and not bool(fact["freshness"]["is_stale"])
        and any(
            support["support_role"] == "supports_absence"
            and support.get("status") == "complete"
            and support.get("target_subject_id") == fact["subject_id"]
            and str(support.get("source_id")) == _OFFICIAL_WEB_SOURCE_ID
            and str(support.get("collector_name")) == _WEBSITE_COLLECTOR_NAME
            and _absence_safe_version(support.get("collector_version"))
            and str(support.get("acquisition_session_id"))
            in absence_safe_sessions
            for support in fact["acquisition_support"]
        )
    )


def _absence_safe_version(version: object) -> bool:
    try:
        parsed = int(str(version))
    except (TypeError, ValueError):
        return False
    return parsed >= _ABSENCE_SAFE_WEBSITE_VERSION


def _fact_evidence(fact: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Evidence entries for a fact's usable supporting observations."""
    merged: dict[str, dict[str, Any]] = {}
    for support in fact["observation_support"]:
        if support.get("support_role") != "supports":
            continue
        if support.get("evidence_status") != "usable":
            continue
        evidence_id = str(support["evidence_id"])
        record = evidence_by_id.get(evidence_id)
        if record is None:
            continue
        entry = merged.setdefault(
            evidence_id,
            {
                "evidence_id": evidence_id,
                "source_id": record["source_id"],
                "source_locator": record["source_locator"],
                "content_sha256": record["content_sha256"],
                "retrieved_at": record["retrieved_at"],
                "acquisition_session_id": record["acquisition_session_id"],
                "collector_name": record["collector_name"],
                "collector_version": record["collector_version"],
                "acquisition_status": record["acquisition_status"],
                "observation_ids": [],
            },
        )
        entry["observation_ids"].append(str(support["observation_id"]))
    for entry in merged.values():
        entry["observation_ids"] = sorted(set(entry["observation_ids"]))
    return sorted(merged.values(), key=lambda item: item["evidence_id"])


def _freshest_evidence(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The freshest entry by retrieval instant (tie-break: evidence id).

    RCJ-04: a current fact confirmed by multiple observations should
    surface its FRESHEST supporting evidence on channel entries, never a
    lexically-first stale row.
    """
    if not entries:
        return None
    return max(
        entries,
        key=lambda entry: (
            _parse_instant(
                entry.get("retrieved_at"),
                field=f"journey evidence {entry.get('evidence_id')} retrieved_at",
            ) or datetime.min.replace(tzinfo=timezone.utc),
            entry["evidence_id"],
        ),
    )


def _load_website_evidence(
    conn: sqlite3.Connection, *, entity_id: str, evaluated_at: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validated official-website evidence with parsed journey metadata.

    CJ-01/CJ-02 — admission and verification are two explicit stages:

    Admission (entity isolation): only website acquisition sessions whose
    acquisition target sits in the canonical Entity's merge lineage are
    considered. Another canonical Entity's sessions are silently out of
    scope — they are neither used nor turned into integrity failures for
    this Entity.

    Verification (producer-grade provenance, RCJ-01/02/03): every
    admitted session must satisfy the website producer's contract —
    src_official_web source, NULL legacy run id, the source-time Entity
    identical in acquisition target and config, the canonical crawl
    config with ALL required keys (extras tolerated so collector version
    bumps do not poison durable history), matching hash and
    deterministic session id, present lifecycle timestamps, a clean
    error state for complete sessions, and stored child counts equal to
    the actual evidence/observation rows. Every evidence row must then
    bind: the producer metadata contract (all required keys), the
    session's source-time entity exactly, a valid page role and channel
    candidates, metadata observation_ids EXACTLY equal to the actual
    attached producer observations, the DETERMINISTIC evidence id over
    (session, final_url, content_sha256), and final_url equal to the
    stored source locator. Failures surface as integrity issues and the
    session/row is never used. An appended child whose counters were
    corrected must still forge the full metadata contract and exact
    observation bindings.
    """
    from ..website.model import canonical_json as website_canonical_json
    from ..website.model import opaque_id as website_opaque_id
    from ..website.model import sha256_text as website_sha256_text

    lineage = _entity_lineage(conn, entity_id)
    lineage_set = set(lineage)

    issues: list[dict[str, Any]] = []
    admitted_session_ids: list[str] = []
    session_status_by_id: dict[str, str] = {}
    session_version_by_id: dict[str, str] = {}
    session_target_by_id: dict[str, str] = {}
    session_start_url_by_id: dict[str, str] = {}

    cursor = conn.execute(
        "SELECT id,target_subject_id,source_id,legacy_run_id,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "evidence_count,observation_count,child_seal_sha256 "
        "FROM acquisition_sessions "
        "WHERE collector_name=? AND status IN ('complete','partial') "
        "ORDER BY id",
        (_WEBSITE_COLLECTOR_NAME,),
    )
    for raw in cursor.fetchall():
        session = {
            description[0]: raw[index]
            for index, description in enumerate(cursor.description or ())
        }
        session_id = str(session["id"])
        target = str(session["target_subject_id"] or "")
        if target not in lineage_set:
            continue  # CJ-01: another Entity's acquisition — not ours
        # R4: versions before the compatible era are silently out of
        # scope for positive journey evidence — not integrity failures.
        if not _positive_evidence_version(session["collector_version"]):
            continue
        # RCJ-02: source-time acquisition identity. The producer writes
        # source_id=src_official_web, legacy_run_id=NULL, and the SAME
        # source-time Entity into target and config. Convergence admits
        # the session through the lineage; it must never weaken the
        # original acquisition identity itself.
        invalid = False
        if str(session["source_id"]) != _OFFICIAL_WEB_SOURCE_ID:
            invalid = True
        if session["legacy_run_id"] is not None:
            invalid = True
        try:
            config = json.loads(str(session["config_json"]))
            if not isinstance(config, dict):
                raise ValueError("config is not a JSON object")
        except ValueError:
            config = None
            invalid = True
        if config is not None:
            # RCJ-01: REQUIRED keys (not exact-set): later collector
            # versions may add config keys without invalidating durable
            # historical sessions. Every required key present, canonical
            # bytes, hash, and the deterministic id still bind the row.
            if not _WEBSITE_SESSION_REQUIRED_CONFIG_KEYS <= set(config):
                invalid = True
            elif str(config.get("entity_id")) != target:
                invalid = True  # RCJ-02: config Entity == acquisition target
            elif str(config.get("entity_id")) not in lineage_set:
                invalid = True
            elif website_canonical_json(config) != str(session["config_json"]):
                invalid = True
            elif website_sha256_text(str(session["config_json"])) != str(
                session["config_hash"]
            ):
                invalid = True
            elif session_id != website_opaque_id(
                "acq", str(config["entity_id"]),
                str(session["started_at"]), str(session["config_hash"]),
            ):
                invalid = True
        if not isinstance(session["started_at"], str) or not session["started_at"]:
            invalid = True
        if not isinstance(session["finished_at"], str) or not session["finished_at"]:
            invalid = True
        if str(session["status"]) == "complete" and session["error"] not in (None, ""):
            invalid = True
        actual_evidence_count = int(conn.execute(
            "SELECT COUNT(*) FROM evidence_items WHERE acquisition_session_id=?",
            (session_id,),
        ).fetchone()[0])
        actual_observation_count = int(conn.execute(
            "SELECT COUNT(*) FROM observations o "
            "JOIN evidence_items e ON e.id=o.evidence_id "
            "WHERE e.acquisition_session_id=?",
            (session_id,),
        ).fetchone()[0])
        if int(session["evidence_count"] or 0) != actual_evidence_count:
            invalid = True
        if int(session["observation_count"] or 0) != actual_observation_count:
            invalid = True
        # XJ-01: the session must carry a child seal, and the seal must
        # match the digest recomputed over the LIVE child set — shape
        # verification is not membership verification. Sealing happens at
        # producer finalization; the migration's triggers make the child
        # set immutable once sealed, and this recompute catches anything
        # that predates or circumvents them.
        from ..storage import session_child_seal_digest

        stored_seal = session["child_seal_sha256"]
        if not isinstance(stored_seal, str) or not stored_seal:
            invalid = True
        elif stored_seal != session_child_seal_digest(conn, session_id):
            invalid = True
        if invalid:
            issues.append(
                {"code": "customer_journey_website_session_invalid",
                 "session_id": session_id}
            )
            continue
        admitted_session_ids.append(session_id)
        session_status_by_id[session_id] = str(session["status"])
        session_version_by_id[session_id] = str(session["collector_version"])
        session_target_by_id[session_id] = target
        # XJ-02: retain the VERIFIED config start_url per session; every
        # row's metadata start_url must bind to it exactly.
        session_start_url_by_id[session_id] = str(config.get("start_url"))

    rows: list[dict[str, Any]] = []
    if not admitted_session_ids:
        return rows, issues
    marks = ",".join("?" for _ in admitted_session_ids)
    evidence_cursor = conn.execute(
        f"SELECT e.id,e.acquisition_session_id,e.source_locator,e.retrieved_at,"
        f"e.content_sha256,e.metadata_json "
        f"FROM evidence_items e "
        f"WHERE e.acquisition_session_id IN ({marks}) "
        f"AND e.source_id=? AND e.source_role='official' "
        f"AND e.status='usable' "
        f"ORDER BY e.id",
        (*admitted_session_ids, _OFFICIAL_WEB_SOURCE_ID),
    )
    for raw in evidence_cursor.fetchall():
        item = {
            description[0]: raw[index]
            for index, description in enumerate(evidence_cursor.description or ())
        }
        evidence_id = str(item["id"])
        row_session_id = str(item["acquisition_session_id"])
        row_invalid = False
        try:
            metadata = json.loads(str(item["metadata_json"]))
            if not isinstance(metadata, dict):
                raise ValueError("metadata is not a JSON object")
        except ValueError:
            metadata = None
            row_invalid = True
        if metadata is not None:
            # RCJ-03: the exact producer metadata contract — every
            # required key the journey consumes must be present (extras
            # tolerated for forward compatibility, RCJ-01).
            if not _WEBSITE_EVIDENCE_REQUIRED_METADATA_KEYS <= set(metadata):
                row_invalid = True
            if metadata.get("acquisition_kind") != "bounded_official_website":
                row_invalid = True
            # XJ-02: the row's source-time site identity binds to the
            # session's VERIFIED config start_url exactly — an unbound
            # metadata value could manufacture external hand-offs.
            if str(metadata.get("start_url")) != session_start_url_by_id.get(
                row_session_id
            ):
                row_invalid = True
            # RCJ-02: the row binds to the session's SOURCE-TIME entity,
            # exactly — not merely somewhere in the lineage.
            if str(metadata.get("entity_id")) != session_target_by_id.get(
                row_session_id
            ):
                row_invalid = True
        final_url = metadata.get("final_url") if metadata else None
        if not isinstance(final_url, str) or not final_url:
            row_invalid = True
        else:
            if final_url != str(item["source_locator"]):
                row_invalid = True
            if evidence_id != website_opaque_id(
                "ev", row_session_id, final_url, str(item["content_sha256"])
            ):
                row_invalid = True
        page_role = metadata.get("page_role") if metadata else None
        if not isinstance(page_role, str) or not page_role:
            row_invalid = True
        raw_channels = metadata.get("channels") if metadata else None
        channels: list[dict[str, Any]] = []
        if isinstance(raw_channels, list):
            for candidate in raw_channels:
                if not isinstance(candidate, dict):
                    row_invalid = True
                    break
                if not all(
                    isinstance(candidate.get(key), str) and candidate[key]
                    for key in (
                        "channel_type",
                        "identifier",
                        "normalized_identifier",
                        "extraction",
                    )
                ):
                    row_invalid = True
                    break
                channels.append(
                    {
                        "channel_type": candidate["channel_type"],
                        "identifier": candidate["identifier"],
                        "normalized_identifier": candidate["normalized_identifier"],
                        "url": candidate.get("url"),
                        "extraction": candidate["extraction"],
                        "canonicalized_channel_id": candidate.get(
                            "canonicalized_channel_id"
                        ),
                        "canonicalized_scope": candidate.get(
                            "canonicalized_scope"
                        ),
                    }
                )
        else:
            row_invalid = True
        # RCJ-03: output seal — the metadata's observation_ids must be
        # EXACTLY the observation rows actually attached to this evidence
        # (sorted producer ids). An appended child that fixes counters
        # must still forge matching observation bindings.
        declared_observation_ids = metadata.get("observation_ids") if metadata else None
        if isinstance(declared_observation_ids, list) and all(
            isinstance(value, str) for value in declared_observation_ids
        ):
            actual_observation_ids = sorted(
                str(row[0])
                for row in conn.execute(
                    "SELECT o.id FROM observations o WHERE o.evidence_id=?",
                    (evidence_id,),
                ).fetchall()
            )
            if sorted(declared_observation_ids) != actual_observation_ids:
                row_invalid = True
        else:
            row_invalid = True
        # R1: PRODUCER-SEMANTIC SEAL. The parser sets booking/ordering/
        # whatsapp detection iff the page carries a channel of that type,
        # and the producer then emits the matching capability observation
        # bound to THIS evidence row. A row declaring an action channel
        # with no matching attached observation is impossible producer
        # output — however perfect its shape — and is never admissible.
        channel_observation_predicates = set()
        for candidate in channels:
            if candidate["channel_type"] == "booking":
                channel_observation_predicates.add("capability.online_booking")
            elif candidate["channel_type"] == "ordering":
                channel_observation_predicates.add("capability.online_ordering")
            elif candidate["channel_type"] == "whatsapp":
                channel_observation_predicates.add("capability.whatsapp")
        if channel_observation_predicates:
            actual_predicates = {
                str(row[0])
                for row in conn.execute(
                    "SELECT o.predicate FROM observations o "
                    "WHERE o.evidence_id=?",
                    (evidence_id,),
                ).fetchall()
            }
            if not channel_observation_predicates <= actual_predicates:
                row_invalid = True
        retrieved_at = _parse_instant(
            item["retrieved_at"],
            field=f"website evidence {evidence_id} retrieved_at",
        )
        if retrieved_at is None:
            row_invalid = True
        if row_invalid:
            issues.append(
                {"code": "customer_journey_website_evidence_invalid",
                 "evidence_id": evidence_id}
            )
            continue
        scope_eligible = metadata.get("business_wide_scope_eligible")
        frontier_exhausted = metadata.get("crawl_frontier_exhausted")
        if not isinstance(scope_eligible, bool):
            row_invalid = True
        if not isinstance(frontier_exhausted, bool):
            row_invalid = True
        start_url = metadata.get("start_url")
        if not isinstance(start_url, str) or not start_url:
            row_invalid = True  # binding below also requires it
        if row_invalid:
            issues.append(
                {"code": "customer_journey_website_evidence_invalid",
                 "evidence_id": evidence_id}
            )
            continue
        age_days = (evaluated_at - retrieved_at).total_seconds() / 86400
        rows.append(
            {
                "evidence_id": evidence_id,
                "source_id": _OFFICIAL_WEB_SOURCE_ID,
                "source_locator": item["source_locator"],
                "content_sha256": item["content_sha256"],
                "retrieved_at": str(item["retrieved_at"]),
                "retrieved_instant": retrieved_at,
                "acquisition_session_id": row_session_id,
                "collector_name": _WEBSITE_COLLECTOR_NAME,
                "collector_version": session_version_by_id.get(
                    row_session_id, ""
                ),
                "acquisition_status": session_status_by_id.get(
                    row_session_id, ""
                ),
                "page_role": page_role,
                "channels": channels,
                # Verified producer flags + the row's SOURCE-TIME site
                # identity (R2/R3): the hand-off same-site test uses this
                # start_url, never today's website fact.
                "business_wide_scope_eligible": scope_eligible,
                "crawl_frontier_exhausted": frontier_exhausted,
                "start_url": start_url,
                "session_start_url": session_start_url_by_id.get(
                    row_session_id
                ),
                "current": 0 <= age_days <= _JOURNEY_EVIDENCE_FRESHNESS_DAYS,
            }
        )
    return rows, issues


def _website_evidence_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": row["evidence_id"],
        "source_id": row["source_id"],
        "source_locator": row["source_locator"],
        "content_sha256": row["content_sha256"],
        "retrieved_at": row["retrieved_at"],
        "acquisition_session_id": row["acquisition_session_id"],
        "collector_name": row["collector_name"],
        "collector_version": row["collector_version"],
        "acquisition_status": row["acquisition_status"],
        "observation_ids": [],
    }


def _channel_entry(
    *,
    channel_type: str,
    identifier: str | None,
    normalized_identifier: str | None,
    scope: str,
    evidence_id: str,
) -> dict[str, Any]:
    return {
        "channel_type": channel_type,
        "identifier": identifier,
        "normalized_identifier": normalized_identifier,
        "scope": scope,
        "evidence_id": evidence_id,
    }


def _website_channel_candidates(
    website_evidence: list[dict[str, Any]], *, channel_type: str, current_only: bool
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(candidate, evidence row) pairs for a channel type, deduplicated by
    normalized identifier, newest evidence first."""
    by_identifier: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for row in sorted(
        website_evidence, key=lambda item: (item["retrieved_instant"], item["evidence_id"])
    ):
        if current_only and not row["current"]:
            continue
        for candidate in row["channels"]:
            if candidate["channel_type"] != channel_type:
                continue
            by_identifier[candidate["normalized_identifier"]] = (candidate, row)
    return sorted(
        by_identifier.values(),
        key=lambda pair: pair[0]["normalized_identifier"],
    )


def _host_of(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _public_surface_coverage(
    stages: list[dict[str, Any]],
    website_evidence: list[dict[str, Any]],
    absence_safe_sessions: frozenset[str],
    website_facts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministic public-surface coverage judgment over the admitted
    website surface. The reconstruction — not the assessment policy —
    owns this decision, so sufficiency mapping never reaches back into
    acquisition tables.

    States:
    - ``evaluation_observed``: the evaluate stage is currently observed;
      ``evidence_ids`` are its admitted evidence identities.
    - ``bounded_inspection_no_evaluation``: no current evaluate evidence,
      but at least one absence-safe website session (complete,
      business-wide-scope-eligible, frontier-exhausted on every admitted
      row) of the entity's CURRENT official website remains current
      across its ENTIRE admitted row set. PCJ-01: every supporting row
      must be current and the row set non-empty — one stale row,
      evaluative or not, disqualifies the session, so stale positive
      evaluation evidence can never convert into a current negative
      coverage judgment. SR-02: the session's verified source-time
      start_url must be same-site (crawler host semantics, so verified
      root->deep redirects on one host qualify) with a CURRENT
      business.website.official fact — a bounded crawl of a superseded
      site must not establish coverage for a different current site,
      and with no current website identity the branch fails closed.
    - ``not_covered``: neither holds; sufficiency must fail closed.

    ``support`` carries the deterministic coverage-provenance rows
    (evidence identity, session, retrieval time) for BOTH branches, so
    assessment chronology and the sealed input signature can include
    coverage evidence that belongs to no journey stage and generated no
    fact (PCJ-02).
    """
    evaluate_stage = next(
        (stage for stage in stages if str(stage.get("stage")) == "evaluate"),
        None,
    )
    if (
        evaluate_stage is not None
        and evaluate_stage.get("evidence_state") == "observed"
    ):
        entries = list(evaluate_stage.get("evidence", ()))
        return {
            "state": "evaluation_observed",
            "evidence_ids": sorted(
                {
                    str(entry.get("evidence_id"))
                    for entry in entries
                }
            ),
            "session_ids": [],
            "support": sorted(
                (
                    {
                        "evidence_id": str(entry.get("evidence_id")),
                        "acquisition_session_id": str(
                            entry.get("acquisition_session_id")
                        ),
                        "retrieved_at": str(entry.get("retrieved_at")),
                    }
                    for entry in entries
                ),
                key=lambda item: item["evidence_id"],
            ),
        }
    rows_by_session: dict[str, list[dict[str, Any]]] = {}
    for row in website_evidence:
        rows_by_session.setdefault(
            str(row["acquisition_session_id"]), []
        ).append(row)
    from ..website.parser import normalize_http_url

    current_site_hosts = set()
    for fact in website_facts:
        if not _fact_current(fact):
            continue
        value = fact.get("value")
        if not isinstance(value, str) or not value:
            continue
        normalized = normalize_http_url(value)
        host = _host_of(normalized) or _host_of(value)
        if host:
            current_site_hosts.add(host)
    if not current_site_hosts:
        # SR-02: without a current official-website identity, no bounded
        # crawl can speak for today's public surface.
        return {
            "state": "not_covered",
            "evidence_ids": [],
            "session_ids": [],
            "support": [],
        }

    def _session_site(rows: list[dict[str, Any]]) -> str | None:
        start = rows[0].get("session_start_url")
        normalized = (
            normalize_http_url(start) if isinstance(start, str) else None
        )
        return _host_of(normalized) or _host_of(start)

    bounded_current = sorted(
        session_id
        for session_id in absence_safe_sessions
        if rows_by_session.get(session_id)
        and all(
            row.get("current")
            for row in rows_by_session[session_id]
        )
        and _session_site(rows_by_session[session_id]) in current_site_hosts
    )
    if bounded_current:
        support_rows = [
            row
            for session_id in bounded_current
            for row in rows_by_session[session_id]
        ]
        return {
            "state": "bounded_inspection_no_evaluation",
            "evidence_ids": [],
            "session_ids": bounded_current,
            "support": sorted(
                (
                    {
                        "evidence_id": str(row["evidence_id"]),
                        "acquisition_session_id": str(
                            row["acquisition_session_id"]
                        ),
                        "retrieved_at": str(row["retrieved_at"]),
                    }
                    for row in support_rows
                ),
                key=lambda item: item["evidence_id"],
            ),
        }
    return {
        "state": "not_covered",
        "evidence_ids": [],
        "session_ids": [],
        "support": [],
    }


def reconstruct_customer_journey(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    facts: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    evaluated_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Deterministically reconstruct the observable customer journey.

    This is a READ-ONLY projection over existing evidence, facts, and
    channels. It judges nothing about journey quality: it records, per
    contract stage, which channels and hand-offs are directly observed,
    which evidence supports them, what remains missing, and an explicit
    evidence state. Two hard invariants: ordering never implies payment,
    and booking/order never implies receipt. Customer reviews never
    establish operational stages (they stay in customer voice).

    Evidence states: observed / not_observed_in_bounded_inspection /
    unknown / stale / conflicted. There is deliberately no `absent`:
    `not_observed` records a bounded inspection that did not find the
    capability, never its absence.
    """
    evaluation = _parse_instant(evaluated_at, field="journey evaluated_at")
    if evaluation is None:
        return (
            {"reconstruction_version": RECONSTRUCTION_VERSION, "stages": [],
             "observed_stage_count": 0, "current_stage_count": 0},
            [{"code": "customer_journey_evaluation_time_invalid"}],
        )

    evidence_by_id = {str(item["id"]): item for item in evidence}
    website_evidence, issues = _load_website_evidence(
        conn, entity_id=entity_id, evaluated_at=evaluation
    )

    facts_by_predicate: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        facts_by_predicate.setdefault(str(fact["predicate"]), []).append(fact)
    facts_by_predicate = {
        predicate: sorted(items, key=lambda fact: str(fact["id"]))
        for predicate, items in facts_by_predicate.items()
    }

    def _fresh_facts(predicate: str) -> list[dict[str, Any]]:
        # CJ-03: supported facts remain contributions regardless of
        # freshness; the fact's own freshness flag then decides currency.
        return [
            fact for fact in facts_by_predicate.get(predicate, [])
            if _supported_fact(fact)
        ]

    def _conflicted_facts(predicates: tuple[str, ...]) -> list[dict[str, Any]]:
        return [
            fact
            for predicate in predicates
            for fact in facts_by_predicate.get(predicate, [])
            if fact["status"] == "conflicted"
        ]

    # ---- contributions per stage -------------------------------------
    # Each contribution: {"current": bool, "channels": [...], "evidence": [...]}
    contributions: dict[str, list[dict[str, Any]]] = {
        stage: [] for stage in STAGE_ORDER
    }

    # discover: verified official website fact.
    website_facts = [
        fact for fact in _fresh_facts("business.website.official")
    ]
    for fact in website_facts:
        value = fact.get("value")
        if not isinstance(value, str) or not value:
            continue
        fact_evidence = _fact_evidence(fact, evidence_by_id)
        if not fact_evidence:
            continue
        contributions["discover"].append(
            {
                "current": _fact_current(fact),
                "channels": [
                    _channel_entry(
                        channel_type="website",
                        identifier=value,
                        normalized_identifier=value,
                        scope="business_entity",
                        evidence_id=_freshest_evidence(fact_evidence)["evidence_id"],
                    )
                ],
                "evidence": fact_evidence,
            }
        )

    # discover: the live Maps listing as a discovery surface.
    maps_anchor_facts = sorted(
        (
            fact
            for predicate in _MAPS_ANCHOR_PREDICATES
            for fact in facts_by_predicate.get(predicate, [])
            if _supported_fact(fact)
            and any(
                support.get("support_role") == "supports"
                and support.get("source_id") == _GOOGLE_MAPS_SOURCE_ID
                and support.get("evidence_status") == "usable"
                for support in fact["observation_support"]
            )
        ),
        key=lambda fact: str(fact["id"]),
    )
    for anchor in maps_anchor_facts:
        # RCJ-06: EVERY supported Maps anchor contributes; the stage is
        # current when ANY current anchor exists, never because of opaque
        # fact-id ordering. Evidence is deduplicated per anchor fact.
        anchor_is_current = _fact_current(anchor)
        maps_evidence = _fact_evidence(anchor, evidence_by_id)
        maps_evidence = [
            entry for entry in maps_evidence
            if entry["source_id"] == _GOOGLE_MAPS_SOURCE_ID
        ]
        if maps_evidence:
            contributions["discover"].append(
                {
                    "current": anchor_is_current,
                    "channels": [
                        _channel_entry(
                            channel_type="maps_listing",
                            identifier=None,
                            normalized_identifier=None,
                            scope="location",
                            evidence_id=(
                                _freshest_evidence(maps_evidence)
                                or maps_evidence[0]
                            )["evidence_id"],
                        )
                    ],
                    "evidence": maps_evidence,
                }
            )

    # discover: the retained home page itself is a discovery surface.
    for row in website_evidence:
        if row["page_role"] != "home":
            continue
        contributions["discover"].append(
            {
                "current": row["current"],
                "channels": [],
                "evidence": [_website_evidence_entry(row)],
            }
        )

    # discover: official social channels presented on the website.
    for channel_type in sorted(_SOCIAL_CHANNEL_TYPES):
        for candidate, row in _website_channel_candidates(
            website_evidence, channel_type=channel_type, current_only=False
        ):
            contributions["discover"].append(
                {
                    "current": row["current"],
                    "channels": [
                        _channel_entry(
                            channel_type=channel_type,
                            identifier=candidate["identifier"],
                            normalized_identifier=candidate["normalized_identifier"],
                            scope=(
                                candidate["canonicalized_scope"]
                                if candidate["canonicalized_scope"]
                                else "website_page"
                            ),
                            evidence_id=row["evidence_id"],
                        )
                    ],
                    "evidence": [_website_evidence_entry(row)],
                }
            )

    # evaluate: reconciled offering facts.
    for fact in _fresh_facts("business.offering.service"):
        fact_evidence = _fact_evidence(fact, evidence_by_id)
        if fact_evidence:
            contributions["evaluate"].append(
                {
                    "current": _fact_current(fact),
                    "channels": [],
                    "evidence": fact_evidence,
                }
            )

    # evaluate: retained offering/about/home pages from the website.
    for row in website_evidence:
        if row["page_role"] not in _EVALUATE_PAGE_ROLES:
            continue
        contributions["evaluate"].append(
            {
                "current": row["current"],
                "channels": [],
                "evidence": [_website_evidence_entry(row)],
            }
        )

    # contact: current phone facts.
    for fact in _fresh_facts("location.phone"):
        value = fact.get("value")
        if not isinstance(value, str) or not value:
            continue
        fact_evidence = _fact_evidence(fact, evidence_by_id)
        if not fact_evidence:
            continue
        contributions["contact"].append(
            {
                "current": _fact_current(fact),
                "channels": [
                    _channel_entry(
                        channel_type="phone",
                        identifier=value,
                        normalized_identifier=value,
                        scope="location",
                        evidence_id=_freshest_evidence(fact_evidence)["evidence_id"],
                    )
                ],
                "evidence": fact_evidence,
            }
        )

    # contact: WhatsApp capability facts.
    for fact in _fresh_facts("capability.whatsapp"):
        fact_evidence = _fact_evidence(fact, evidence_by_id)
        if not fact_evidence:
            continue
        contributions["contact"].append(
            {
                "current": _fact_current(fact),
                "channels": [
                    _channel_entry(
                        channel_type="whatsapp",
                        identifier=None,
                        normalized_identifier=None,
                        scope="business_entity",
                        evidence_id=_freshest_evidence(fact_evidence)["evidence_id"],
                    )
                ],
                "evidence": fact_evidence,
            }
        )

    # contact: contact pages and email channels from the website.
    for row in website_evidence:
        if row["page_role"] == "contact":
            contributions["contact"].append(
                {
                    "current": row["current"],
                    "channels": [],
                    "evidence": [_website_evidence_entry(row)],
                }
            )
    for candidate, row in _website_channel_candidates(
        website_evidence, channel_type="email", current_only=False
    ):
        contributions["contact"].append(
            {
                "current": row["current"],
                "channels": [
                    _channel_entry(
                        channel_type="email",
                        identifier=candidate["identifier"],
                        normalized_identifier=candidate["normalized_identifier"],
                        scope="website_page",
                        evidence_id=row["evidence_id"],
                    )
                ],
                "evidence": [_website_evidence_entry(row)],
            }
        )

    # book_order: booking/ordering capabilities and transaction types.
    for predicate, channel_type in (
        ("capability.online_booking", "booking"),
        ("capability.online_ordering", "ordering"),
    ):
        for fact in _fresh_facts(predicate):
            fact_evidence = _fact_evidence(fact, evidence_by_id)
            if not fact_evidence:
                continue
            contributions["book_order"].append(
                {
                    "current": _fact_current(fact),
                    "channels": [
                        _channel_entry(
                            channel_type=channel_type,
                            identifier=None,
                            normalized_identifier=None,
                            scope="business_entity",
                            evidence_id=_freshest_evidence(fact_evidence)["evidence_id"],
                        )
                    ],
                    "evidence": fact_evidence,
                }
            )
    for fact in _fresh_facts("business.model.transaction_type"):
        value = fact.get("value")
        if value not in ("booking", "ordering"):
            continue
        fact_evidence = _fact_evidence(fact, evidence_by_id)
        if not fact_evidence:
            continue
        contributions["book_order"].append(
            {
                "current": _fact_current(fact),
                "channels": [],
                "evidence": fact_evidence,
            }
        )
    for channel_type in ("booking", "ordering"):
        for candidate, row in _website_channel_candidates(
            website_evidence, channel_type=channel_type, current_only=False
        ):
            contributions["book_order"].append(
                {
                    "current": row["current"],
                    "channels": [
                        _channel_entry(
                            channel_type=channel_type,
                            identifier=candidate["identifier"],
                            normalized_identifier=candidate["normalized_identifier"],
                            scope=(
                                candidate["canonicalized_scope"]
                                if candidate["canonicalized_scope"]
                                else "website_page"
                            ),
                            evidence_id=row["evidence_id"],
                        )
                    ],
                    "evidence": [_website_evidence_entry(row)],
                }
            )

    # support: retained support pages and support channels.
    for row in website_evidence:
        if row["page_role"] == "support":
            contributions["support"].append(
                {
                    "current": row["current"],
                    "channels": [],
                    "evidence": [_website_evidence_entry(row)],
                }
            )
    for candidate, row in _website_channel_candidates(
        website_evidence, channel_type="support", current_only=False
    ):
        contributions["support"].append(
            {
                "current": row["current"],
                "channels": [
                    _channel_entry(
                        channel_type="support",
                        identifier=candidate["identifier"],
                        normalized_identifier=candidate["normalized_identifier"],
                        scope="website_page",
                        evidence_id=row["evidence_id"],
                    )
                ],
                "evidence": [_website_evidence_entry(row)],
            }
        )

    # ---- bounded absence for book_order ------------------------------
    # R2: sessions that actually satisfy the producer's absence contract
    # — producer-verified by the journey reader, status complete, and
    # EVERY retained row reports business-wide scope eligibility and
    # frontier exhaustion (the producer writes these flags per row,
    # constant across one crawl).
    # XJ-03: bounded not_observed is the result of the COMPLETE bounded
    # attempt, so the entire supporting session must be trustworthy:
    # every evidence child of the session must have passed row
    # verification (no invalid rows, none outside the
    # official/usable envelope), and all surviving rows must report
    # scope eligibility and frontier exhaustion.
    sessions_with_row_issues = {
        str(issue.get("session_id"))
        for issue in issues
    }
    # YJ-02: integrity accounting is scoped to THIS entity's admitted
    # sessions only (the sole candidates for absence safety) and uses
    # ONE grouped query — no database-wide session scan, no N+1.
    admitted_session_ids = sorted({
        row["acquisition_session_id"] for row in website_evidence
    })
    total_children_by_session = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT acquisition_session_id, COUNT(*) FROM evidence_items "
            f"WHERE acquisition_session_id IN ({','.join('?' for _ in admitted_session_ids)}) "
            "GROUP BY acquisition_session_id",
            tuple(admitted_session_ids),
        )
    } if admitted_session_ids else {}
    admitted_children_by_session: dict[str, int] = {}
    for row in website_evidence:
        key = row["acquisition_session_id"]
        admitted_children_by_session[key] = (
            admitted_children_by_session.get(key, 0) + 1
        )
    sessions_with_invalid_rows = {
        session_id
        for session_id in admitted_session_ids
        if total_children_by_session.get(session_id, 0)
        != admitted_children_by_session.get(session_id, 0)
    }
    absence_safe_sessions = frozenset(
        session_id
        for session_id in sorted({
            row["acquisition_session_id"] for row in website_evidence
        })
        if session_id not in sessions_with_invalid_rows
        and session_id not in sessions_with_row_issues
        and all(
            row["acquisition_status"] == "complete"
            and row["business_wide_scope_eligible"]
            and row["crawl_frontier_exhausted"]
            for row in website_evidence
            if row["acquisition_session_id"] == session_id
        )
    )
    booking_absent = any(
        _bounded_absence_current(fact, absence_safe_sessions)
        for fact in facts_by_predicate.get("capability.online_booking", [])
    )
    ordering_absent = any(
        _bounded_absence_current(fact, absence_safe_sessions)
        for fact in facts_by_predicate.get("capability.online_ordering", [])
    )
    book_order_bounded_absence = booking_absent and ordering_absent

    # ---- hand-offs (directly observed only) ---------------------------
    handoffs: list[dict[str, Any]] = []
    handoff_currency: dict[tuple[str, str, str], bool] = {}
    website_handoff_channels: dict[str, list[dict[str, Any]]] = {
        "book_order": [], "contact": [],
    }
    website_handoff_channel_currency: dict[tuple[str, str], bool] = {}

    for channel_type, stage in (
        ("booking", "book_order"),
        ("ordering", "book_order"),
        ("whatsapp", "contact"),
    ):
        # RCJ-05: hand-offs are constructed over ALL retained website
        # evidence, each carrying its currency. Observed stages keep
        # current hand-offs; stale stages keep their historical ones.
        for candidate, row in _website_channel_candidates(
            website_evidence, channel_type=channel_type, current_only=False
        ):
            # R3: the same-site/external decision uses the row's VERIFIED
            # SOURCE-TIME site (producer start_url), never today's
            # website fact — a later domain move must not reclassify a
            # historical same-site action link as an external hand-off.
            from ..website.parser import normalize_http_url

            bound_start = row.get("session_start_url")
            normalized_start = (
                normalize_http_url(bound_start)
                if isinstance(bound_start, str) else None
            )
            source_time_host = _host_of(normalized_start) or _host_of(
                bound_start
            )
            target_host = _host_of(candidate.get("url"))
            if source_time_host and target_host:
                if target_host == source_time_host:
                    # Same-site action link at source time: booking
                    # evidence, not a hand-off to an external endpoint.
                    continue
            handoffs.append(
                {
                    "from": "official_website",
                    "to": channel_type,
                    "evidence_id": row["evidence_id"],
                    "current": row["current"],
                }
            )
            handoff_currency[
                ("official_website", channel_type, row["evidence_id"])
            ] = row["current"]
            website_handoff_channels[stage].append((candidate, row))
            website_handoff_channel_currency[
                (candidate["normalized_identifier"], row["evidence_id"])
            ] = row["current"]
    maps_to_website: list[dict[str, Any]] = []
    maps_to_website_current = False
    # Prefer a CURRENT website fact for the hand-off; fall back to the
    # freshest supported stale fact so a stale discover stage keeps its
    # historical Maps->website transition (RCJ-05).
    ordered_website_facts = sorted(
        website_facts,
        key=lambda fact: (_fact_current(fact), str(fact["id"])),
        reverse=True,
    )
    for fact in ordered_website_facts:
        maps_support = [
            support
            for support in fact["observation_support"]
            if support.get("support_role") == "supports"
            and support.get("source_id") == _GOOGLE_MAPS_SOURCE_ID
            and support.get("evidence_status") == "usable"
        ]
        if maps_support:
            maps_to_website = _fact_evidence(fact, evidence_by_id)
            maps_to_website = [
                entry for entry in maps_to_website
                if entry["source_id"] == _GOOGLE_MAPS_SOURCE_ID
            ]
            if maps_to_website:
                maps_to_website_current = _fact_current(fact)
                maps_handoff_entry = (
                    _freshest_evidence(maps_to_website) or maps_to_website[0]
                )
                handoffs.append(
                    {
                        "from": "maps_listing",
                        "to": "official_website",
                        "evidence_id": maps_handoff_entry["evidence_id"],
                        "current": maps_to_website_current,
                    }
                )
                handoff_currency[
                    (
                        "maps_listing",
                        "official_website",
                        maps_handoff_entry["evidence_id"],
                    )
                ] = maps_to_website_current
            break

    # ---- stage assembly ------------------------------------------------
    stage_conflict_predicates: dict[str, tuple[str, ...]] = {
        "discover": ("business.website.official",),
        "evaluate": ("business.offering.service",),
        "contact": ("location.phone", "capability.whatsapp"),
        "book_order": (
            "capability.online_booking",
            "capability.online_ordering",
            "business.model.transaction_type",
        ),
        "pay": (),
        "receive": (),
        "support": (),
        "return": (),
    }

    stages: list[dict[str, Any]] = []
    for stage in STAGE_ORDER:
        stage_contributions = contributions[stage]
        conflicted = _conflicted_facts(stage_conflict_predicates[stage])
        has_current = any(item["current"] for item in stage_contributions)
        has_any = bool(stage_contributions)

        if conflicted:
            evidence_state = "conflicted"
        elif has_current:
            evidence_state = "observed"
        elif has_any:
            evidence_state = "stale"
        elif stage == "book_order" and book_order_bounded_absence:
            evidence_state = "not_observed_in_bounded_inspection"
        else:
            evidence_state = "unknown"

        # CJ-04: an observed stage exposes only its CURRENT surface —
        # current channels, hand-offs, and evidence. A stale stage keeps
        # its historical surface, qualified by the stage state itself.
        if evidence_state == "observed":
            stage_contributions = [
                item for item in stage_contributions if item["current"]
            ]
        channels: list[dict[str, Any]] = []
        seen_channels: set[tuple[str, str | None, str]] = set()
        evidence_entries: dict[str, dict[str, Any]] = {}
        for item in sorted(
            stage_contributions,
            key=lambda item: (
                tuple(
                    (channel["channel_type"], channel["normalized_identifier"] or "")
                    for channel in item["channels"]
                ),
                item["evidence"][0]["evidence_id"] if item["evidence"] else "",
            ),
        ):
            for channel in item["channels"]:
                key = (
                    channel["channel_type"],
                    channel["normalized_identifier"],
                    channel["evidence_id"],
                )
                if key in seen_channels:
                    continue
                seen_channels.add(key)
                channels.append(channel)
            for entry in item["evidence"]:
                existing = evidence_entries.get(entry["evidence_id"])
                if existing is None:
                    evidence_entries[entry["evidence_id"]] = entry
                else:
                    merged_ids = sorted(
                        set(existing["observation_ids"])
                        | set(entry["observation_ids"])
                    )
                    existing["observation_ids"] = merged_ids

        if stage == "book_order":
            # RCJ-05: observed stages merge only CURRENT hand-off
            # channels; stale stages keep their historical ones.
            handoff_candidates = [
                (candidate, row)
                for candidate, row in website_handoff_channels.get(
                    "book_order", []
                )
                if evidence_state != "observed"
                or website_handoff_channel_currency.get(
                    (candidate["normalized_identifier"], row["evidence_id"]),
                    False,
                )
            ]
            for candidate, row in handoff_candidates:
                channel = _channel_entry(
                    channel_type=candidate["channel_type"],
                    identifier=candidate["identifier"],
                    normalized_identifier=candidate["normalized_identifier"],
                    scope=(
                        candidate["canonicalized_scope"]
                        if candidate["canonicalized_scope"]
                        else "website_page"
                    ),
                    evidence_id=row["evidence_id"],
                )
                key = (
                    channel["channel_type"],
                    channel["normalized_identifier"],
                    channel["evidence_id"],
                )
                if key not in seen_channels:
                    seen_channels.add(key)
                    channels.append(channel)
                evidence_entries.setdefault(
                    row["evidence_id"], _website_evidence_entry(row)
                )
        if stage == "contact":
            handoff_candidates = [
                (candidate, row)
                for candidate, row in website_handoff_channels.get(
                    "contact", []
                )
                if evidence_state != "observed"
                or website_handoff_channel_currency.get(
                    (candidate["normalized_identifier"], row["evidence_id"]),
                    False,
                )
            ]
            for candidate, row in handoff_candidates:
                channel = _channel_entry(
                    channel_type="whatsapp",
                    identifier=candidate["identifier"],
                    normalized_identifier=candidate["normalized_identifier"],
                    scope="website_page",
                    evidence_id=row["evidence_id"],
                )
                key = (
                    channel["channel_type"],
                    channel["normalized_identifier"],
                    channel["evidence_id"],
                )
                if key not in seen_channels:
                    seen_channels.add(key)
                    channels.append(channel)
                evidence_entries.setdefault(
                    row["evidence_id"], _website_evidence_entry(row)
                )

        stage_handoffs = [
            handoff for handoff in handoffs
            if (
                stage == "discover" and handoff["from"] == "maps_listing"
            ) or (
                stage in ("book_order", "contact")
                and handoff["from"] == "official_website"
                and (
                    (stage == "book_order" and handoff["to"] in ("booking", "ordering"))
                    or (stage == "contact" and handoff["to"] == "whatsapp")
                )
            )
        ]
        if stage == "discover" and maps_to_website:
            for entry in maps_to_website:
                evidence_entries.setdefault(entry["evidence_id"], entry)

        # RCJ-04: no independent evidence re-aging. A contribution's
        # currency follows its SOURCE policy — fact-derived entries
        # inherit the fact's per-predicate freshness window (a 60-day
        # phone fact keeps day-45 Maps evidence current); metadata-derived
        # entries use the journey window. The observed-stage filter below
        # already kept only current contributions, so their evidence,
        # channels, and hand-offs are current BY CONSTRUCTION and are not
        # re-aged against a second universal window.
        if evidence_state == "observed":
            retained_ids = {
                entry_id
                for item in stage_contributions
                for entry_id in (
                    entry["evidence_id"] for entry in item["evidence"]
                )
            }
            channels = [
                channel for channel in channels
                if channel["evidence_id"] in retained_ids
                or website_handoff_channel_currency.get(
                    (
                        channel["normalized_identifier"],
                        channel["evidence_id"],
                    ),
                    False,
                )
            ]
            stage_handoffs = [
                handoff for handoff in stage_handoffs
                if handoff_currency.get(
                    (
                        handoff["from"],
                        handoff["to"],
                        handoff["evidence_id"],
                    ),
                    False,
                )
            ]


        missing_knowledge: list[str] = []
        if evidence_state == "unknown" and stage in _MISSING_KNOWLEDGE:
            missing_knowledge = list(_MISSING_KNOWLEDGE[stage])
        elif evidence_state == "unknown":
            missing_knowledge = [
                "no evidence for this stage is retained by any v1 source"
            ]
        elif evidence_state == "not_observed_in_bounded_inspection":
            missing_knowledge = [
                "booking and ordering were not observed in a bounded, "
                "frontier-exhausted website inspection; this records the "
                "inspection, not the absence of the capability"
            ]
        elif evidence_state == "stale":
            missing_knowledge = [
                "supporting evidence exists but is no longer current"
            ]

        # An identifier-bearing channel supersedes an identifier-less
        # fact-derived entry of the same type in the same stage (e.g. a
        # WhatsApp capability fact plus the wa.me link actually retained).
        identified_types = {
            channel["channel_type"]
            for channel in channels
            if channel["normalized_identifier"]
        }
        channels = [
            channel for channel in channels
            if channel["normalized_identifier"]
            or channel["channel_type"] not in identified_types
        ]

        stages.append(
            {
                "stage": stage,
                "stage_label": _STAGE_LABELS[stage],
                "evidence_state": evidence_state,
                "channels": sorted(
                    channels,
                    key=lambda channel: (
                        channel["channel_type"],
                        channel["normalized_identifier"] or "",
                        channel["evidence_id"],
                    ),
                ),
                "handoffs": sorted(
                    stage_handoffs,
                    key=lambda handoff: (
                        handoff["from"], handoff["to"], handoff["evidence_id"]
                    ),
                ),
                "evidence": sorted(
                    evidence_entries.values(),
                    key=lambda entry: entry["evidence_id"],
                ),
                "missing_knowledge": missing_knowledge,
            }
        )

    handoffs.sort(
        key=lambda handoff: (
            handoff["from"], handoff["to"], handoff["evidence_id"]
        )
    )
    observed_stage_count = sum(
        1 for stage in stages
        if stage["evidence_state"] in ("observed", "stale")
    )
    current_stage_count = sum(
        1 for stage in stages if stage["evidence_state"] == "observed"
    )
    return (
        {
            "reconstruction_version": RECONSTRUCTION_VERSION,
            "stages": stages,
            "handoffs": handoffs,
            "observed_stage_count": observed_stage_count,
            "current_stage_count": current_stage_count,
            "public_surface_coverage": _public_surface_coverage(
                stages, website_evidence, absence_safe_sessions,
                website_facts,
            ),
        },
        issues,
    )
