from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .crawl import origin_url, page_role
from .model import (
    CAPABILITY_PREDICATES,
    COLLECTOR_NAME,
    COLLECTOR_VERSION,
    OFFICIAL_WEB_SOURCE_ID,
    CrawlResult,
    PageCapture,
    WebsiteAcquisitionError,
    canonical_json,
    opaque_id,
    sha256_text,
)
from .parser import ChannelCandidate, normalize_http_url
from .reconcile import reconcile_not_observed, reconcile_observation_group


_BUSINESS_WIDE_HOME_CHANNEL_TYPES = {
    "instagram",
    "facebook",
    "linkedin",
    "x",
    "tiktok",
    "youtube",
    "booking",
    "ordering",
    "support",
}


def _website_channel(home_url: str) -> ChannelCandidate:
    return ChannelCandidate(
        channel_type="website",
        identifier=home_url,
        normalized_identifier=home_url,
        url=home_url,
        extraction="verified_official_fact",
    )


def _timestamp_key(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WebsiteAcquisitionError(
            f"{field} is not a valid ISO-8601 timestamp: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WebsiteAcquisitionError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _latest_timestamp(values: list[str], *, field: str) -> str:
    if not values:
        raise WebsiteAcquisitionError(f"{field} has no timestamps")
    return max(values, key=lambda value: _timestamp_key(value, field=field))


def _is_home_capture(
    capture: PageCapture, *, canonical_home_url: str | None
) -> bool:
    if canonical_home_url is None:
        return False
    return normalize_http_url(capture.final_url) == normalize_http_url(
        canonical_home_url
    )


def _business_wide_scope_eligible(
    *, start_url: str, canonical_home_url: str | None
) -> bool:
    start = normalize_http_url(start_url)
    if start is None:
        return False
    if start == origin_url(start):
        return True
    if canonical_home_url is None:
        return False
    canonical = normalize_http_url(canonical_home_url)
    return canonical is not None and canonical == origin_url(canonical)


def _candidate_is_business_wide(
    candidate: ChannelCandidate, *, home_page: bool, scope_eligible: bool
) -> bool:
    return (
        home_page
        and scope_eligible
        and candidate.channel_type in _BUSINESS_WIDE_HOME_CHANNEL_TYPES
    )


def _ensure_channel(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    candidate: ChannelCandidate,
    verified_at: str,
) -> tuple[str, bool, bool]:
    rows = conn.execute(
        "SELECT c.id,c.business_entity_id,c.location_id,c.channel_type,c.identifier,"
        "c.normalized_identifier,c.url,c.status,ks.kind,ks.record_state "
        "FROM channels c JOIN knowledge_subjects ks ON ks.id=c.id "
        "WHERE c.business_entity_id=? AND c.channel_type=? AND c.normalized_identifier=? "
        "ORDER BY c.id",
        (entity_id, candidate.channel_type, candidate.normalized_identifier),
    ).fetchall()
    if len(rows) > 1:
        raise WebsiteAcquisitionError(
            f"multiple channels match {candidate.channel_type}:{candidate.normalized_identifier}"
        )
    if rows:
        row = rows[0]
        if row[2] is not None:
            raise WebsiteAcquisitionError(
                "business-wide official-site channel collides with location-specific channel"
            )
        if row[1] != entity_id or row[3] != candidate.channel_type:
            raise WebsiteAcquisitionError("existing channel ownership/type is incompatible")
        if row[4] != candidate.identifier or row[5] != candidate.normalized_identifier:
            raise WebsiteAcquisitionError("existing channel endpoint identity is incompatible")
        if row[8] != "channel" or row[9] != "active":
            raise WebsiteAcquisitionError("existing channel subject is not active channel state")
        conn.execute(
            "UPDATE channels SET url=?,status='active',last_verified_at=?,updated_at=? WHERE id=?",
            (candidate.url, verified_at, verified_at, row[0]),
        )
        return str(row[0]), False, True

    channel_id = opaque_id(
        "ch", entity_id, candidate.channel_type, candidate.normalized_identifier
    )
    if conn.execute(
        "SELECT 1 FROM knowledge_subjects WHERE id=?", (channel_id,)
    ).fetchone() is not None:
        raise WebsiteAcquisitionError(f"deterministic channel id collision: {channel_id}")
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) "
        "VALUES (?,'channel',?,?)",
        (channel_id, verified_at, verified_at),
    )
    conn.execute(
        "INSERT INTO channels("
        "id,business_entity_id,location_id,channel_type,identifier,normalized_identifier,url,status,"
        "first_observed_at,last_verified_at,created_at,updated_at"
        ") VALUES (?,?,NULL,?,?,?,?, 'active',?,?,?,?)",
        (
            channel_id,
            entity_id,
            candidate.channel_type,
            candidate.identifier,
            candidate.normalized_identifier,
            candidate.url,
            verified_at,
            verified_at,
            verified_at,
            verified_at,
        ),
    )
    return channel_id, True, False


def _observation(
    *,
    entity_id: str,
    evidence_id: str,
    predicate: str,
    value: Any,
    observation_kind: str,
    extraction_method: str,
    confidence: float,
    multi: bool = False,
) -> dict[str, Any]:
    value_json = canonical_json(value)
    value_hash = sha256_text(value_json)
    return {
        "id": opaque_id("obs", evidence_id, predicate, value_hash),
        "subject_id": entity_id,
        "predicate": predicate,
        "evidence_id": evidence_id,
        "value_json": value_json,
        "normalized_value_json": value_json,
        "value_hash": value_hash,
        "fact_slot": value_hash if multi else "__single__",
        "observation_kind": observation_kind,
        "extraction_method": extraction_method,
        "confidence": confidence,
    }


def _observations_for_page(
    *,
    entity_id: str,
    evidence_id: str,
    capture: PageCapture,
    canonical_home_url: str | None,
    home_page: bool,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if home_page and canonical_home_url is not None:
        result.append(
            _observation(
                entity_id=entity_id,
                evidence_id=evidence_id,
                predicate="business.website.official",
                value=canonical_home_url,
                observation_kind="structured_value",
                extraction_method="deterministic_parser",
                confidence=1.0,
            )
        )
    if capture.parsed.whatsapp_detected:
        result.append(
            _observation(
                entity_id=entity_id,
                evidence_id=evidence_id,
                predicate="capability.whatsapp",
                value=True,
                observation_kind="detected_capability",
                extraction_method="deterministic_parser",
                confidence=1.0,
            )
        )
    if capture.parsed.booking_detected:
        result.extend(
            [
                _observation(
                    entity_id=entity_id,
                    evidence_id=evidence_id,
                    predicate="capability.online_booking",
                    value=True,
                    observation_kind="detected_capability",
                    extraction_method="heuristic",
                    confidence=0.8,
                ),
                _observation(
                    entity_id=entity_id,
                    evidence_id=evidence_id,
                    predicate="business.model.transaction_type",
                    value="booking",
                    observation_kind="derived_observation",
                    extraction_method="heuristic",
                    confidence=0.8,
                    multi=True,
                ),
            ]
        )
    if capture.parsed.ordering_detected:
        result.extend(
            [
                _observation(
                    entity_id=entity_id,
                    evidence_id=evidence_id,
                    predicate="capability.online_ordering",
                    value=True,
                    observation_kind="detected_capability",
                    extraction_method="heuristic",
                    confidence=0.8,
                ),
                _observation(
                    entity_id=entity_id,
                    evidence_id=evidence_id,
                    predicate="business.model.transaction_type",
                    value="ordering",
                    observation_kind="derived_observation",
                    extraction_method="heuristic",
                    confidence=0.8,
                    multi=True,
                ),
            ]
        )
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in result:
        unique[(str(item["predicate"]), str(item["value_hash"]))] = item
    return list(unique.values())


def _evidence_id(session_id: str, capture: PageCapture) -> str:
    return opaque_id("ev", session_id, capture.final_url, capture.content_sha256)


def _page_channels(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    capture: PageCapture,
    home_page: bool,
    scope_eligible: bool,
) -> tuple[list[dict[str, Any]], int, int]:
    metadata: list[dict[str, Any]] = []
    created = refreshed = 0
    for candidate in capture.parsed.channels:
        item: dict[str, Any] = {
            "channel_type": candidate.channel_type,
            "identifier": candidate.identifier,
            "normalized_identifier": candidate.normalized_identifier,
            "url": candidate.url,
            "extraction": candidate.extraction,
            "canonicalized_channel_id": None,
            "canonicalized_scope": None,
        }
        if _candidate_is_business_wide(
            candidate, home_page=home_page, scope_eligible=scope_eligible
        ):
            channel_id, was_created, was_refreshed = _ensure_channel(
                conn,
                entity_id=entity_id,
                candidate=candidate,
                verified_at=capture.retrieved_at,
            )
            item["canonicalized_channel_id"] = channel_id
            item["canonicalized_scope"] = "business_entity"
            created += int(was_created)
            refreshed += int(was_refreshed)
        metadata.append(item)
    return metadata, created, refreshed


def ingest_crawl_result(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    entity_id: str,
    start_url: str,
    result: CrawlResult,
    finished_at: str,
) -> dict[str, Any]:
    if not result.captures:
        raise WebsiteAcquisitionError("cannot ingest an empty website crawl")
    status = (
        "partial"
        if result.errors or result.canonical_home_url is None
        else "complete"
    )
    scope_eligible = _business_wide_scope_eligible(
        start_url=start_url, canonical_home_url=result.canonical_home_url
    )
    evidence_created = observations_created = channels_created = channels_refreshed = 0
    facts_created = facts_replaced = supports_created = absence_created = 0
    observed_predicates: set[str] = set()

    try:
        conn.execute("BEGIN IMMEDIATE")
        if result.canonical_home_url is not None:
            _channel_id, was_created, was_refreshed = _ensure_channel(
                conn,
                entity_id=entity_id,
                candidate=_website_channel(result.canonical_home_url),
                verified_at=finished_at,
            )
            channels_created += int(was_created)
            channels_refreshed += int(was_refreshed)

        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        observed_times: dict[tuple[str, str], list[str]] = {}
        for capture in result.captures:
            evidence_id = _evidence_id(session_id, capture)
            home_page = _is_home_capture(
                capture, canonical_home_url=result.canonical_home_url
            )
            channel_metadata, created, refreshed = _page_channels(
                conn,
                entity_id=entity_id,
                capture=capture,
                home_page=home_page,
                scope_eligible=scope_eligible,
            )
            channels_created += created
            channels_refreshed += refreshed
            specs = _observations_for_page(
                entity_id=entity_id,
                evidence_id=evidence_id,
                capture=capture,
                canonical_home_url=result.canonical_home_url,
                home_page=home_page,
            )
            metadata_json = canonical_json(
                {
                    "acquisition_kind": "bounded_official_website",
                    "entity_id": entity_id,
                    "start_url": start_url,
                    "requested_url": capture.requested_url,
                    "final_url": capture.final_url,
                    "crawl_depth": capture.depth,
                    "page_role": page_role(capture.final_url),
                    "home_page": home_page,
                    "business_wide_scope_eligible": scope_eligible,
                    "crawl_frontier_exhausted": result.frontier_exhausted,
                    "title": capture.parsed.title,
                    "canonical_url": capture.parsed.canonical_url,
                    "channels": channel_metadata,
                    "observation_ids": sorted(str(item["id"]) for item in specs),
                }
            )
            conn.execute(
                "INSERT INTO evidence_items("
                "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
                "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
                ") VALUES (?,?,?,?,'official','usable',?,NULL,NULL,?,?,?,?,?)",
                (
                    evidence_id,
                    session_id,
                    OFFICIAL_WEB_SOURCE_ID,
                    capture.final_url,
                    capture.retrieved_at,
                    capture.media_type,
                    capture.content_sha256,
                    capture.artifact_ref,
                    metadata_json,
                    finished_at,
                ),
            )
            evidence_created += 1
            for spec in specs:
                conn.execute(
                    "INSERT INTO observations("
                    "id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,"
                    "observation_kind,observed_at,extracted_at,extraction_method,extractor_name,"
                    "extractor_version,confidence,created_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        spec["id"],
                        spec["subject_id"],
                        spec["predicate"],
                        spec["evidence_id"],
                        spec["value_json"],
                        spec["normalized_value_json"],
                        spec["value_hash"],
                        spec["observation_kind"],
                        capture.retrieved_at,
                        finished_at,
                        spec["extraction_method"],
                        COLLECTOR_NAME,
                        COLLECTOR_VERSION,
                        spec["confidence"],
                        finished_at,
                    ),
                )
                observations_created += 1
                observed_predicates.add(str(spec["predicate"]))
                key = (str(spec["predicate"]), str(spec["fact_slot"]))
                groups.setdefault(key, []).append(spec)
                observed_times.setdefault(key, []).append(capture.retrieved_at)

        for (predicate, fact_slot), specs in sorted(groups.items()):
            created, replaced, links = reconcile_observation_group(
                conn,
                entity_id=entity_id,
                predicate=predicate,
                fact_slot=fact_slot,
                observations=specs,
                observed_at=_latest_timestamp(
                    observed_times[(predicate, fact_slot)],
                    field=f"{predicate} observation group",
                ),
                reconciled_at=finished_at,
            )
            facts_created += created
            facts_replaced += replaced
            supports_created += links

        if status == "complete" and scope_eligible:
            for predicate in CAPABILITY_PREDICATES:
                if predicate in observed_predicates:
                    continue
                created, support = reconcile_not_observed(
                    conn,
                    entity_id=entity_id,
                    predicate=predicate,
                    session_id=session_id,
                    observed_at=finished_at,
                )
                absence_created += created
                supports_created += support

        partial_reasons = list(result.errors)
        if result.canonical_home_url is None:
            partial_reasons.append("canonical home page was not established")
        error = None if status == "complete" else "; ".join(partial_reasons)[:4000]
        cursor = conn.execute(
            "UPDATE acquisition_sessions SET status=?,finished_at=?,error=?,evidence_count=?,"
            "observation_count=? WHERE id=? AND status='running'",
            (
                status,
                finished_at,
                error,
                evidence_created,
                observations_created,
                session_id,
            ),
        )
        if cursor.rowcount != 1:
            raise WebsiteAcquisitionError(
                f"website acquisition session {session_id} changed during finalization"
            )
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise WebsiteAcquisitionError(
                f"foreign-key violations after website acquisition: {violations!r}"
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    unresolved_candidates = [
        "business.offering.service",
        "business.customer_segment.stated",
        *(
            predicate
            for predicate in CAPABILITY_PREDICATES
            if predicate not in observed_predicates
        ),
    ]
    unresolved = tuple(dict.fromkeys(unresolved_candidates))
    return {
        "status": status,
        "evidence_items_created": evidence_created,
        "observations_created": observations_created,
        "channels_created": channels_created,
        "channels_refreshed": channels_refreshed,
        "facts_created": facts_created,
        "facts_replaced": facts_replaced,
        "fact_support_links_created": supports_created,
        "not_observed_facts_created": absence_created,
        "unresolved_predicates": unresolved,
    }
