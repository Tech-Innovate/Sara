from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..dossier.core import DossierQueryError, resolve_selection
from ..maps_backfill import _fetch_one
from ..understanding_vocabulary import VocabularySeedError, verify_business_understanding_vocabulary
from .crawl import page_role
from .model import (
    CAPABILITY_PREDICATES,
    COLLECTOR_NAME,
    COLLECTOR_VERSION,
    OFFICIAL_WEB_SOURCE_ID,
    RECONCILIATION_VERSION,
    CrawlConfig,
    CrawlResult,
    PageCapture,
    WebsiteAcquisitionError,
    canonical_json,
    opaque_id,
    sha256_text,
)
from .parser import ChannelCandidate, normalize_http_url


def _parse_json(value: object, *, label: str) -> Any:
    if not isinstance(value, str):
        raise WebsiteAcquisitionError(f"{label} is not stored as JSON text")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise WebsiteAcquisitionError(f"{label} contains malformed JSON") from exc


def _ensure_source(conn: sqlite3.Connection, created_at: str) -> None:
    row = _fetch_one(
        conn,
        "SELECT id,source_type,active FROM sources WHERE id=?",
        (OFFICIAL_WEB_SOURCE_ID,),
    )
    if row is None:
        conn.execute(
            "INSERT INTO sources(id,source_type,name,base_url,created_at,active) "
            "VALUES (?,'official_web','Official website',NULL,?,1)",
            (OFFICIAL_WEB_SOURCE_ID, created_at),
        )
        return
    if row["id"] != OFFICIAL_WEB_SOURCE_ID or row["source_type"] != "official_web":
        raise WebsiteAcquisitionError(
            f"official website source identity/type drift: database={row!r}"
        )
    if int(row["active"]) != 1:
        raise WebsiteAcquisitionError("official website source is inactive")


def resolve_verified_site(
    conn: sqlite3.Connection,
    *,
    business_id: int | None = None,
    canonical_key: str | None = None,
    entity_id: str | None = None,
) -> tuple[str, str]:
    try:
        verify_business_understanding_vocabulary(conn)
        resolved_entity, _selection = resolve_selection(
            conn,
            business_id=business_id,
            canonical_key=canonical_key,
            entity_id=entity_id,
        )
    except (VocabularySeedError, DossierQueryError) as exc:
        raise WebsiteAcquisitionError(str(exc)) from exc

    fact = _fetch_one(
        conn,
        "SELECT id,value_json,status FROM facts "
        "WHERE subject_id=? AND predicate='business.website.official' "
        "AND fact_slot='__single__' AND valid_to IS NULL",
        (resolved_entity,),
    )
    if fact is None:
        raise WebsiteAcquisitionError(
            "no current business.website.official fact exists; domain discovery is outside Phase 6"
        )
    if fact["status"] not in {"confirmed", "single_source", "stale"}:
        raise WebsiteAcquisitionError(
            f"official website fact is not usable as a verified acquisition start: {fact['status']!r}"
        )
    value = _parse_json(fact["value_json"], label="official website fact")
    if not isinstance(value, str):
        raise WebsiteAcquisitionError("official website fact is not a URL string")
    start_url = normalize_http_url(value)
    if start_url is None:
        raise WebsiteAcquisitionError("official website fact is not an absolute HTTP(S) URL")

    supports = conn.execute(
        "SELECT COUNT(*),COUNT(DISTINCT e.source_id) "
        "FROM fact_observation_support fos "
        "JOIN observations o ON o.id=fos.observation_id "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE fos.fact_id=? AND fos.support_role='supports' AND e.status='usable'",
        (fact["id"],),
    ).fetchone()
    contradictions = int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_observation_support "
            "WHERE fact_id=? AND support_role='contradicts'",
            (fact["id"],),
        ).fetchone()[0]
    )
    if int(supports[0]) < 1 or int(supports[1]) < 1 or contradictions:
        raise WebsiteAcquisitionError(
            "official website fact lacks clean usable supporting provenance"
        )
    return resolved_entity, start_url


def begin_session(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    start_url: str,
    evidence_root: Path,
    config: CrawlConfig,
    started_at: str,
) -> str:
    config_payload = {
        "entity_id": entity_id,
        "start_url": start_url,
        "page_limit": config.page_limit,
        "depth_limit": config.depth_limit,
        "max_response_bytes": config.max_response_bytes,
        "timeout_seconds": config.timeout_seconds,
        "user_agent": config.user_agent,
        "obey_robots": config.obey_robots,
        "evidence_root": str(evidence_root),
    }
    config_json = canonical_json(config_payload)
    config_hash = sha256_text(config_json)
    session_id = opaque_id("acq", entity_id, started_at, config_hash)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_source(conn, started_at)
        if conn.execute(
            "SELECT 1 FROM acquisition_sessions WHERE id=?", (session_id,)
        ).fetchone() is not None:
            raise WebsiteAcquisitionError(
                f"deterministic website acquisition session collision: {session_id}"
            )
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,"
            "status,started_at,finished_at,error,legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,'running',?,NULL,NULL,NULL,0,0)",
            (
                session_id,
                entity_id,
                OFFICIAL_WEB_SOURCE_ID,
                COLLECTOR_NAME,
                COLLECTOR_VERSION,
                config_json,
                config_hash,
                started_at,
            ),
        )
        conn.commit()
        return session_id
    except BaseException:
        conn.rollback()
        raise


def fail_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    status: str,
    finished_at: str,
    error: str,
) -> None:
    if status not in {"blocked", "failed", "cancelled"}:
        raise ValueError("invalid terminal website session status")
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE acquisition_sessions SET status=?,finished_at=?,error=? "
            "WHERE id=? AND status='running'",
            (status, finished_at, error[:4000], session_id),
        )
        if cursor.rowcount != 1:
            raise WebsiteAcquisitionError(
                f"website acquisition session {session_id} is not running"
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _website_channel(home_url: str) -> ChannelCandidate:
    return ChannelCandidate(
        channel_type="website",
        identifier=home_url,
        normalized_identifier=home_url.lower(),
        url=home_url,
        extraction="verified_official_fact",
    )


def _candidate_is_business_wide(
    candidate: ChannelCandidate, *, role: str, first_page: bool
) -> bool:
    if candidate.channel_type in {"instagram", "facebook", "linkedin", "x", "tiktok", "youtube"}:
        return first_page or role in {"contact", "support"}
    if candidate.channel_type in {"email", "phone", "whatsapp"}:
        return first_page or role in {"contact", "support"}
    if candidate.channel_type == "booking":
        return first_page or role in {"booking", "contact"}
    if candidate.channel_type == "ordering":
        return first_page or role in {"ordering", "contact"}
    if candidate.channel_type == "support":
        return first_page or role in {"support", "contact"}
    return False


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
    first_page: bool,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if first_page and canonical_home_url is not None:
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
        result.append(
            _observation(
                entity_id=entity_id,
                evidence_id=evidence_id,
                predicate="capability.online_booking",
                value=True,
                observation_kind="detected_capability",
                extraction_method="heuristic",
                confidence=0.8,
            )
        )
        result.append(
            _observation(
                entity_id=entity_id,
                evidence_id=evidence_id,
                predicate="business.model.transaction_type",
                value="booking",
                observation_kind="derived_observation",
                extraction_method="heuristic",
                confidence=0.8,
                multi=True,
            )
        )
    if capture.parsed.ordering_detected:
        result.append(
            _observation(
                entity_id=entity_id,
                evidence_id=evidence_id,
                predicate="capability.online_ordering",
                value=True,
                observation_kind="detected_capability",
                extraction_method="heuristic",
                confidence=0.8,
            )
        )
        result.append(
            _observation(
                entity_id=entity_id,
                evidence_id=evidence_id,
                predicate="business.model.transaction_type",
                value="ordering",
                observation_kind="derived_observation",
                extraction_method="heuristic",
                confidence=0.8,
                multi=True,
            )
        )
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in result:
        unique[(str(item["predicate"]), str(item["value_hash"]))] = item
    return list(unique.values())


def _current_fact(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    predicate: str,
    fact_slot: str,
) -> dict[str, Any] | None:
    return _fetch_one(
        conn,
        "SELECT id,value_json,normalized_value_json,value_hash,status,valid_from,"
        "last_verified_at,reconciliation_version FROM facts "
        "WHERE subject_id=? AND predicate=? AND fact_slot=? AND valid_to IS NULL",
        (entity_id, predicate, fact_slot),
    )


def _usable_supports(conn: sqlite3.Connection, fact_id: str) -> list[dict[str, str]]:
    cursor = conn.execute(
        "SELECT o.id,e.source_id,o.value_json FROM fact_observation_support fos "
        "JOIN observations o ON o.id=fos.observation_id "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE fos.fact_id=? AND fos.support_role='supports' AND e.status='usable' "
        "ORDER BY o.id",
        (fact_id,),
    )
    return [
        {
            "observation_id": str(row[0]),
            "source_id": str(row[1]),
            "value_json": str(row[2]),
        }
        for row in cursor.fetchall()
    ]


def _insert_fact(
    conn: sqlite3.Connection,
    *,
    fact_id: str,
    entity_id: str,
    predicate: str,
    fact_slot: str,
    value_json: str | None,
    value_hash: str | None,
    status: str,
    valid_from: str,
    reconciled_at: str,
) -> None:
    conn.execute(
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,status,"
        "valid_from,valid_to,last_verified_at,reconciled_at,reconciliation_version,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
        (
            fact_id,
            entity_id,
            predicate,
            fact_slot,
            value_json,
            value_json,
            value_hash,
            status,
            valid_from,
            valid_from,
            reconciled_at,
            RECONCILIATION_VERSION,
            reconciled_at,
        ),
    )


def _close_fact(conn: sqlite3.Connection, fact_id: str, valid_to: str) -> None:
    cursor = conn.execute(
        "UPDATE facts SET valid_to=? WHERE id=? AND valid_to IS NULL",
        (valid_to, fact_id),
    )
    if cursor.rowcount != 1:
        raise WebsiteAcquisitionError(
            f"current fact changed during website reconciliation: {fact_id}"
        )


def _link_support(
    conn: sqlite3.Connection, *, fact_id: str, observation_id: str, role: str
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO fact_observation_support(fact_id,observation_id,support_role) "
        "VALUES (?,?,?)",
        (fact_id, observation_id, role),
    )
    return int(conn.execute("SELECT changes()").fetchone()[0] == 1)


def _reconcile_group(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    predicate: str,
    fact_slot: str,
    observations: list[dict[str, Any]],
    observed_at: str,
    reconciled_at: str,
) -> tuple[int, int, int]:
    if not observations:
        return 0, 0, 0
    value_json = str(observations[0]["value_json"])
    value_hash = str(observations[0]["value_hash"])
    if any(item["value_json"] != value_json for item in observations):
        raise WebsiteAcquisitionError(
            f"website observation group disagrees internally for {predicate}"
        )

    definition = _fetch_one(
        conn,
        "SELECT cardinality,reconciliation_policy FROM predicate_definitions "
        "WHERE name=? AND active=1",
        (predicate,),
    )
    if definition is None:
        raise WebsiteAcquisitionError(
            f"website extractor emitted uncontrolled predicate {predicate!r}"
        )
    if definition["cardinality"] == "single" and fact_slot != "__single__":
        raise WebsiteAcquisitionError(
            f"single-valued website predicate {predicate!r} has invalid slot"
        )
    if definition["cardinality"] == "multi" and fact_slot != value_hash:
        raise WebsiteAcquisitionError(
            f"multi-valued website predicate {predicate!r} has invalid slot"
        )
    if definition["reconciliation_policy"] not in {
        "prefer_authoritative_recent",
        "latest_observed",
        "supported_set",
    }:
        raise WebsiteAcquisitionError(
            "website collector does not implement reconciliation policy "
            f"{definition['reconciliation_policy']!r}"
        )

    current = _current_fact(
        conn, entity_id=entity_id, predicate=predicate, fact_slot=fact_slot
    )
    new_ids = [str(item["id"]) for item in observations]
    if current is None:
        fact_id = opaque_id(
            "fact", entity_id, predicate, fact_slot, value_hash, observed_at
        )
        _insert_fact(
            conn,
            fact_id=fact_id,
            entity_id=entity_id,
            predicate=predicate,
            fact_slot=fact_slot,
            value_json=value_json,
            value_hash=value_hash,
            status="single_source",
            valid_from=observed_at,
            reconciled_at=reconciled_at,
        )
        links = sum(
            _link_support(
                conn, fact_id=fact_id, observation_id=observation_id, role="supports"
            )
            for observation_id in new_ids
        )
        return 1, 0, links

    previous = _usable_supports(conn, str(current["id"]))
    previous_sources = {item["source_id"] for item in previous}
    same_value = current["value_json"] == value_json and current["value_hash"] == value_hash
    last_verified = str(current["last_verified_at"] or current["valid_from"])

    if str(observed_at) < str(current["valid_from"]):
        # Preserve late-arriving historical observations without rewriting current
        # fact time. Only same-source/same-value evidence can safely join a
        # single_source fact without changing the fact's immutable status.
        if same_value and (
            current["status"] == "confirmed"
            or previous_sources == {OFFICIAL_WEB_SOURCE_ID}
        ):
            links = sum(
                _link_support(
                    conn,
                    fact_id=str(current["id"]),
                    observation_id=observation_id,
                    role="supports",
                )
                for observation_id in new_ids
            )
            return 0, 0, links
        return 0, 0, 0

    if same_value and str(observed_at) <= last_verified:
        if current["status"] == "confirmed" or previous_sources == {OFFICIAL_WEB_SOURCE_ID}:
            links = sum(
                _link_support(
                    conn,
                    fact_id=str(current["id"]),
                    observation_id=observation_id,
                    role="supports",
                )
                for observation_id in new_ids
            )
            return 0, 0, links
        return 0, 0, 0

    # Newer evidence creates a new immutable fact version so last_verified_at,
    # status, and provenance stay internally consistent.
    _close_fact(conn, str(current["id"]), observed_at)
    fact_id = opaque_id(
        "fact", entity_id, predicate, fact_slot, value_hash, observed_at
    )
    same_value_old = [
        item for item in previous if item["value_json"] == value_json
    ]
    support_sources = {item["source_id"] for item in same_value_old}
    support_sources.add(OFFICIAL_WEB_SOURCE_ID)
    if same_value and current["status"] == "confirmed":
        status = "confirmed"
    else:
        status = "confirmed" if len(support_sources) >= 2 else "single_source"
    _insert_fact(
        conn,
        fact_id=fact_id,
        entity_id=entity_id,
        predicate=predicate,
        fact_slot=fact_slot,
        value_json=value_json,
        value_hash=value_hash,
        status=status,
        valid_from=observed_at,
        reconciled_at=reconciled_at,
    )
    links = 0
    for item in same_value_old:
        links += _link_support(
            conn,
            fact_id=fact_id,
            observation_id=item["observation_id"],
            role="supports",
        )
    for observation_id in new_ids:
        links += _link_support(
            conn,
            fact_id=fact_id,
            observation_id=observation_id,
            role="supports",
        )
    if not same_value:
        for item in previous:
            if item["value_json"] != value_json:
                links += _link_support(
                    conn,
                    fact_id=fact_id,
                    observation_id=item["observation_id"],
                    role="contradicts",
                )
    return 1, 1, links


def _reconcile_not_observed(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    predicate: str,
    session_id: str,
    observed_at: str,
) -> tuple[int, int]:
    current = _current_fact(
        conn, entity_id=entity_id, predicate=predicate, fact_slot="__single__"
    )
    if current is not None:
        if current["status"] in {"conflicted", "not_applicable"}:
            return 0, 0
        current_value = (
            None
            if current["value_json"] is None
            else _parse_json(current["value_json"], label=f"current {predicate} fact")
        )
        if current_value is not None:
            return 0, 0
        if str(observed_at) < str(current["valid_from"]):
            return 0, 0
        _close_fact(conn, str(current["id"]), observed_at)

    fact_id = opaque_id(
        "fact", entity_id, predicate, "__single__", "not_observed", observed_at
    )
    _insert_fact(
        conn,
        fact_id=fact_id,
        entity_id=entity_id,
        predicate=predicate,
        fact_slot="__single__",
        value_json=None,
        value_hash=None,
        status="not_observed",
        valid_from=observed_at,
        reconciled_at=observed_at,
    )
    conn.execute(
        "INSERT INTO fact_acquisition_support(fact_id,acquisition_session_id,support_role) "
        "VALUES (?,?,'supports_absence')",
        (fact_id, session_id),
    )
    return 1, 1


def _evidence_id(session_id: str, capture: PageCapture) -> str:
    return opaque_id("ev", session_id, capture.final_url, capture.content_sha256)


def _page_channel_metadata(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    capture: PageCapture,
    first_page: bool,
    verified_at: str,
) -> tuple[list[dict[str, Any]], int, int]:
    role = page_role(capture.final_url)
    metadata: list[dict[str, Any]] = []
    created = refreshed = 0
    for candidate in capture.parsed.channels:
        row: dict[str, Any] = {
            "channel_type": candidate.channel_type,
            "identifier": candidate.identifier,
            "normalized_identifier": candidate.normalized_identifier,
            "url": candidate.url,
            "extraction": candidate.extraction,
            "canonicalized_channel_id": None,
            "canonicalized_scope": None,
        }
        if _candidate_is_business_wide(candidate, role=role, first_page=first_page):
            channel_id, was_created, was_refreshed = _ensure_channel(
                conn,
                entity_id=entity_id,
                candidate=candidate,
                verified_at=verified_at,
            )
            row["canonicalized_channel_id"] = channel_id
            row["canonicalized_scope"] = "business_entity"
            created += int(was_created)
            refreshed += int(was_refreshed)
        metadata.append(row)
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
    status = "partial" if result.errors else "complete"
    evidence_created = observations_created = channels_created = channels_refreshed = 0
    facts_created = facts_replaced = supports_created = absence_created = 0
    observed_predicates: set[str] = set()

    try:
        conn.execute("BEGIN IMMEDIATE")
        # The verified official home is a business-wide channel regardless of
        # which page exposed other endpoints.
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
        for index, capture in enumerate(result.captures):
            evidence_id = _evidence_id(session_id, capture)
            channel_metadata, created, refreshed = _page_channel_metadata(
                conn,
                entity_id=entity_id,
                capture=capture,
                first_page=index == 0,
                verified_at=capture.retrieved_at,
            )
            channels_created += created
            channels_refreshed += refreshed
            specs = _observations_for_page(
                entity_id=entity_id,
                evidence_id=evidence_id,
                capture=capture,
                canonical_home_url=result.canonical_home_url,
                first_page=index == 0,
            )
            metadata_json = canonical_json(
                {
                    "acquisition_kind": "bounded_official_website",
                    "entity_id": entity_id,
                    "requested_url": capture.requested_url,
                    "final_url": capture.final_url,
                    "crawl_depth": capture.depth,
                    "page_role": page_role(capture.final_url),
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
            created, replaced, links = _reconcile_group(
                conn,
                entity_id=entity_id,
                predicate=predicate,
                fact_slot=fact_slot,
                observations=specs,
                observed_at=max(observed_times[(predicate, fact_slot)]),
                reconciled_at=finished_at,
            )
            facts_created += created
            facts_replaced += replaced
            supports_created += links

        if status == "complete":
            for predicate in CAPABILITY_PREDICATES:
                if predicate in observed_predicates:
                    continue
                created, support = _reconcile_not_observed(
                    conn,
                    entity_id=entity_id,
                    predicate=predicate,
                    session_id=session_id,
                    observed_at=finished_at,
                )
                absence_created += created
                supports_created += support

        error = None if not result.errors else "; ".join(result.errors)[:4000]
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

    unresolved = tuple(
        predicate
        for predicate in (
            "business.offering.service",
            "business.customer_segment.stated",
        )
        if predicate not in observed_predicates
    )
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
