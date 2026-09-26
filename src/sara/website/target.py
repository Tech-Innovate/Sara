from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..dossier.core import DossierQueryError, resolve_selection
from ..maps_backfill import _fetch_one
from ..understanding_vocabulary import VocabularySeedError, verify_business_understanding_vocabulary
from .model import (
    COLLECTOR_NAME,
    COLLECTOR_VERSION,
    OFFICIAL_WEB_SOURCE_ID,
    CrawlConfig,
    WebsiteAcquisitionError,
    canonical_json,
    opaque_id,
    sha256_text,
)
from .parser import normalize_http_url


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
    config_json = canonical_json(
        {
            "entity_id": entity_id,
            "start_url": start_url,
            "page_limit": config.page_limit,
            "depth_limit": config.depth_limit,
            "max_response_bytes": config.max_response_bytes,
            "timeout_seconds": config.timeout_seconds,
            "request_interval_seconds": config.request_interval_seconds,
            "max_policy_delay_seconds": config.max_policy_delay_seconds,
            "retry_attempt_limit": config.retry_attempt_limit,
            "retry_base_delay_seconds": config.retry_base_delay_seconds,
            "retry_max_delay_seconds": config.retry_max_delay_seconds,
            "retry_delay_budget_seconds": config.retry_delay_budget_seconds,
            "user_agent": config.user_agent,
            "obey_robots": config.obey_robots,
            "evidence_root": str(evidence_root),
        }
    )
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
