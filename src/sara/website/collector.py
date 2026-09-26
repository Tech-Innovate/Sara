from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from ..dossier.core import DossierQueryError, resolve_selection
from ..maps_backfill import _fetch_one
from ..migrations import MigrationError, apply_migrations
from ..storage import connect_existing
from ..understanding_vocabulary import (
    VocabularySeedError,
    seed_business_understanding_vocabulary,
    verify_business_understanding_vocabulary,
)
from .http import HttpResponse, SafeHttpClient, WebsiteBlockedError, WebsiteFetchError
from .parser import ChannelCandidate, ParsedPage, normalize_http_url, parse_html, same_site


class WebsiteAcquisitionError(RuntimeError):
    """Official website acquisition cannot proceed safely."""


OFFICIAL_WEB_SOURCE_ID = "src_official_web"
COLLECTOR_NAME = "sara.website"
COLLECTOR_VERSION = "1"
RECONCILIATION_VERSION = "official-web-v1"
_ID_NAMESPACE = "sara.business-understanding.official-web.v1"
_CAPABILITY_PREDICATES = (
    "capability.online_booking",
    "capability.online_ordering",
    "capability.whatsapp",
)


@dataclass(frozen=True)
class CrawlConfig:
    page_limit: int = 8
    depth_limit: int = 2
    max_response_bytes: int = 1_048_576
    timeout_seconds: float = 10.0
    user_agent: str = "SaraBusinessUnderstanding/1.0"
    obey_robots: bool = True

    def validate(self) -> None:
        if self.page_limit < 1 or self.page_limit > 50:
            raise ValueError("page_limit must be between 1 and 50")
        if self.depth_limit < 0 or self.depth_limit > 5:
            raise ValueError("depth_limit must be between 0 and 5")
        if self.max_response_bytes < 16_384 or self.max_response_bytes > 10_485_760:
            raise ValueError("max_response_bytes must be between 16384 and 10485760")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 120:
            raise ValueError("timeout_seconds must be greater than zero and at most 120")
        if not self.user_agent.strip():
            raise ValueError("user_agent must not be blank")
        if not self.obey_robots:
            raise ValueError("Phase-6 website acquisition requires robots/source-policy compliance")


@dataclass(frozen=True)
class PageCapture:
    requested_url: str
    final_url: str
    depth: int
    retrieved_at: str
    status: int
    media_type: str
    charset: str
    headers: dict[str, str]
    body: bytes
    content_sha256: str
    artifact_ref: str
    parsed: ParsedPage


@dataclass(frozen=True)
class WebsiteAcquisitionStats:
    session_id: str
    business_entity_id: str
    start_url: str
    canonical_home_url: str | None
    status: str
    pages_fetched: int
    evidence_items_created: int
    observations_created: int
    channels_created: int
    channels_refreshed: int
    facts_created: int
    facts_replaced: int
    fact_support_links_created: int
    not_observed_facts_created: int
    fetch_errors: tuple[str, ...]
    unresolved_predicates: tuple[str, ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _opaque_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join((_ID_NAMESPACE, *(str(part) for part in parts)))
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


def _parse_json_value(value: object, *, label: str) -> Any:
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


def _official_website_fact(conn: sqlite3.Connection, entity_id: str) -> tuple[str, dict[str, Any]]:
    row = _fetch_one(
        conn,
        "SELECT id,value_json,status,valid_from,last_verified_at FROM facts "
        "WHERE subject_id=? AND predicate='business.website.official' "
        "AND fact_slot='__single__' AND valid_to IS NULL",
        (entity_id,),
    )
    if row is None:
        raise WebsiteAcquisitionError(
            "no current business.website.official fact exists; domain discovery is outside Phase 6"
        )
    if row["status"] not in {"confirmed", "single_source", "stale"}:
        raise WebsiteAcquisitionError(
            f"official website fact is not usable as a verified acquisition start: {row['status']!r}"
        )
    value = _parse_json_value(row["value_json"], label="official website fact")
    if not isinstance(value, str):
        raise WebsiteAcquisitionError("official website fact is not a URL string")
    normalized = normalize_http_url(value)
    if normalized is None:
        raise WebsiteAcquisitionError("official website fact is not an absolute HTTP(S) URL")

    supports = conn.execute(
        "SELECT COUNT(*),COUNT(DISTINCT e.source_id) "
        "FROM fact_observation_support fos "
        "JOIN observations o ON o.id=fos.observation_id "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE fos.fact_id=? AND fos.support_role='supports' AND e.status='usable'",
        (row["id"],),
    ).fetchone()
    contradictions = int(
        conn.execute(
            "SELECT COUNT(*) FROM fact_observation_support WHERE fact_id=? AND support_role='contradicts'",
            (row["id"],),
        ).fetchone()[0]
    )
    if int(supports[0]) < 1 or int(supports[1]) < 1 or contradictions:
        raise WebsiteAcquisitionError(
            "official website fact lacks clean usable supporting provenance"
        )
    return normalized, row


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    return urlunsplit((parsed.scheme, netloc, "/", "", ""))


def _artifact_path(
    root: Path, *, entity_id: str, session_id: str, final_url: str, content_sha256: str
) -> Path:
    url_hash = _sha256_text(final_url)[:12]
    return root / entity_id / session_id / f"{content_sha256[:24]}-{url_hash}.html"


def _write_artifact(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _page_role(url: str) -> str:
    path = urlsplit(url).path.lower()
    for role, terms in (
        ("booking", ("book", "booking", "reserve", "reservation", "appointment")),
        ("ordering", ("order", "ordering", "delivery", "pickup")),
        ("contact", ("contact",)),
        ("offerings", ("service", "services", "product", "products", "menu", "pricing")),
        ("locations", ("location", "locations", "branches")),
        ("about", ("about",)),
        ("support", ("support", "help", "faq")),
        ("careers", ("career", "careers", "jobs")),
    ):
        if any(term in path for term in terms):
            return role
    return "home" if path in {"", "/"} else "other"


def _crawl(
    *,
    entity_id: str,
    session_id: str,
    start_url: str,
    evidence_root: Path,
    config: CrawlConfig,
    client: SafeHttpClient,
    now: Callable[[], str],
) -> tuple[list[PageCapture], list[str], str | None]:
    queue: list[tuple[int, int, str]] = []
    heapq.heappush(queue, (-10_000, 0, start_url))
    root = _origin(start_url)
    if root != start_url:
        heapq.heappush(queue, (-9_000, 0, root))
    seen: set[str] = set()
    captures: list[PageCapture] = []
    errors: list[str] = []
    canonical_home: str | None = None

    while queue and len(captures) < config.page_limit:
        _negative_priority, depth, url = heapq.heappop(queue)
        normalized = normalize_http_url(url)
        if normalized is None or normalized in seen or depth > config.depth_limit:
            continue
        seen.add(normalized)
        try:
            response: HttpResponse = client.fetch(normalized)
        except (WebsiteBlockedError, WebsiteFetchError) as exc:
            errors.append(str(exc))
            continue

        parsed = parse_html(response.final_url, response.text)
        digest = _sha256_bytes(response.body)
        artifact = _artifact_path(
            evidence_root,
            entity_id=entity_id,
            session_id=session_id,
            final_url=response.final_url,
            content_sha256=digest,
        )
        _write_artifact(artifact, response.body)
        retrieved_at = now()
        captures.append(
            PageCapture(
                requested_url=response.requested_url,
                final_url=response.final_url,
                depth=depth,
                retrieved_at=retrieved_at,
                status=response.status,
                media_type=response.media_type,
                charset=response.charset,
                headers=response.headers,
                body=response.body,
                content_sha256=digest,
                artifact_ref=str(artifact),
                parsed=parsed,
            )
        )
        if len(captures) == 1:
            candidate = parsed.canonical_url
            if candidate is not None and same_site(candidate, start_url):
                canonical_home = candidate
            else:
                canonical_home = response.final_url

        if depth >= config.depth_limit:
            continue
        for link in parsed.links:
            if not same_site(link.url, start_url):
                continue
            if link.url in seen:
                continue
            heapq.heappush(queue, (-link.priority, depth + 1, link.url))

    return captures, errors, canonical_home


def _channel_id(entity_id: str, channel: ChannelCandidate) -> str:
    return _opaque_id("ch", entity_id, channel.channel_type, channel.normalized_identifier)


def _website_channel(home_url: str) -> ChannelCandidate:
    return ChannelCandidate(
        channel_type="website",
        identifier=home_url,
        normalized_identifier=home_url.lower(),
        url=home_url,
        extraction="verified_official_fact",
    )


def _ensure_channel(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    channel: ChannelCandidate,
    observed_at: str,
) -> tuple[str, bool, bool]:
    matches = conn.execute(
        "SELECT c.id,c.business_entity_id,c.location_id,c.channel_type,c.identifier,"
        "c.normalized_identifier,c.url,c.status,ks.kind,ks.record_state "
        "FROM channels c JOIN knowledge_subjects ks ON ks.id=c.id "
        "WHERE c.business_entity_id=? AND c.channel_type=? AND c.normalized_identifier=? "
        "ORDER BY c.id",
        (entity_id, channel.channel_type, channel.normalized_identifier),
    ).fetchall()
    if len(matches) > 1:
        raise WebsiteAcquisitionError(
            f"multiple existing channels match {channel.channel_type}:{channel.normalized_identifier}"
        )
    if matches:
        row = matches[0]
        if row[1] != entity_id or row[2] is not None or row[3] != channel.channel_type:
            raise WebsiteAcquisitionError("existing channel ownership/type is incompatible")
        if row[4] != channel.identifier or row[5] != channel.normalized_identifier:
            raise WebsiteAcquisitionError("existing channel endpoint identity is incompatible")
        if row[8] != "channel" or row[9] != "active":
            raise WebsiteAcquisitionError("existing channel subject is not active channel state")
        conn.execute(
            "UPDATE channels SET url=?,status='active',last_verified_at=?,updated_at=? WHERE id=?",
            (channel.url, observed_at, observed_at, row[0]),
        )
        return str(row[0]), False, True

    channel_id = _channel_id(entity_id, channel)
    collision = _fetch_one(conn, "SELECT kind FROM knowledge_subjects WHERE id=?", (channel_id,))
    if collision is not None:
        raise WebsiteAcquisitionError(f"deterministic channel id collision: {channel_id}")
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) "
        "VALUES (?,'channel',?,?)",
        (channel_id, observed_at, observed_at),
    )
    conn.execute(
        "INSERT INTO channels("
        "id,business_entity_id,location_id,channel_type,identifier,normalized_identifier,url,status,"
        "first_observed_at,last_verified_at,created_at,updated_at"
        ") VALUES (?,?,NULL,?,?,?,?, 'active',?,?,?,?)",
        (
            channel_id,
            entity_id,
            channel.channel_type,
            channel.identifier,
            channel.normalized_identifier,
            channel.url,
            observed_at,
            observed_at,
            observed_at,
            observed_at,
        ),
    )
    return channel_id, True, False


def _observation_spec(
    *,
    entity_id: str,
    evidence_id: str,
    predicate: str,
    value: Any,
    kind: str,
    method: str,
    confidence: float,
) -> dict[str, Any]:
    value_json = _canonical_json(value)
    value_hash = _sha256_text(value_json)
    return {
        "id": _opaque_id("obs", evidence_id, predicate, value_hash),
        "subject_id": entity_id,
        "predicate": predicate,
        "evidence_id": evidence_id,
        "value_json": value_json,
        "normalized_value_json": value_json,
        "value_hash": value_hash,
        "fact_slot": "__single__",
        "observation_kind": kind,
        "extraction_method": method,
        "confidence": confidence,
    }


def _page_observations(
    *,
    entity_id: str,
    evidence_id: str,
    capture: PageCapture,
    canonical_home: str | None,
    first_page: bool,
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    if first_page and canonical_home is not None:
        specs.append(
            _observation_spec(
                entity_id=entity_id,
                evidence_id=evidence_id,
                predicate="business.website.official",
                value=canonical_home,
                kind="structured_value",
                method="deterministic_parser",
                confidence=1.0,
            )
        )
    if capture.parsed.whatsapp_detected:
        specs.append(
            _observation_spec(
                entity_id=entity_id,
                evidence_id=evidence_id,
                predicate="capability.whatsapp",
                value=True,
                kind="detected_capability",
                method="deterministic_parser",
                confidence=1.0,
            )
        )
    if capture.parsed.booking_detected:
        specs.append(
            _observation_spec(
                entity_id=entity_id,
                evidence_id=evidence_id,
                predicate="capability.online_booking",
                value=True,
                kind="detected_capability",
                method="heuristic",
                confidence=0.8,
            )
        )
        transaction = _observation_spec(
            entity_id=entity_id,
            evidence_id=evidence_id,
            predicate="business.model.transaction_type",
            value="booking",
            kind="derived_observation",
            method="heuristic",
            confidence=0.8,
        )
        transaction["fact_slot"] = transaction["value_hash"]
        specs.append(transaction)
    if capture.parsed.ordering_detected:
        specs.append(
            _observation_spec(
                entity_id=entity_id,
                evidence_id=evidence_id,
                predicate="capability.online_ordering",
                value=True,
                kind="detected_capability",
                method="heuristic",
                confidence=0.8,
            )
        )
        transaction = _observation_spec(
            entity_id=entity_id,
            evidence_id=evidence_id,
            predicate="business.model.transaction_type",
            value="ordering",
            kind="derived_observation",
            method="heuristic",
            confidence=0.8,
        )
        transaction["fact_slot"] = transaction["value_hash"]
        specs.append(transaction)
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for spec in specs:
        unique[(str(spec["predicate"]), str(spec["value_hash"]))] = spec
    return list(unique.values())


def _current_fact(
    conn: sqlite3.Connection, *, subject_id: str, predicate: str, fact_slot: str
) -> dict[str, Any] | None:
    return _fetch_one(
        conn,
        "SELECT id,value_json,normalized_value_json,value_hash,status,valid_from,last_verified_at,"
        "reconciliation_version FROM facts "
        "WHERE subject_id=? AND predicate=? AND fact_slot=? AND valid_to IS NULL",
        (subject_id, predicate, fact_slot),
    )


def _usable_supports(conn: sqlite3.Connection, fact_id: str) -> list[dict[str, str]]:
    cursor = conn.execute(
        "SELECT o.id,e.source_id,o.value_json FROM fact_observation_support fos "
        "JOIN observations o ON o.id=fos.observation_id "
        "JOIN evidence_items e ON e.id=o.evidence_id "
        "WHERE fos.fact_id=? AND fos.support_role='supports' AND e.status='usable' ORDER BY o.id",
        (fact_id,),
    )
    return [
        {"observation_id": str(row[0]), "source_id": str(row[1]), "value_json": str(row[2])}
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
        ") VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?)",
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
        raise WebsiteAcquisitionError(f"current fact changed during website reconciliation: {fact_id}")


def _reconcile_observation_group(
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
        raise WebsiteAcquisitionError(f"website observation group disagrees internally for {predicate}")

    predicate_row = _fetch_one(
        conn,
        "SELECT cardinality,reconciliation_policy FROM predicate_definitions WHERE name=? AND active=1",
        (predicate,),
    )
    if predicate_row is None:
        raise WebsiteAcquisitionError(f"website extractor emitted uncontrolled predicate {predicate!r}")
    if predicate_row["cardinality"] == "single" and fact_slot != "__single__":
        raise WebsiteAcquisitionError(f"single-valued website predicate {predicate!r} has invalid slot")
    if predicate_row["cardinality"] == "multi" and fact_slot != value_hash:
        raise WebsiteAcquisitionError(f"multi-valued website predicate {predicate!r} has invalid slot")
    if predicate_row["reconciliation_policy"] not in {
        "prefer_authoritative_recent",
        "latest_observed",
        "supported_set",
    }:
        raise WebsiteAcquisitionError(
            f"website collector does not implement reconciliation policy {predicate_row['reconciliation_policy']!r}"
        )

    current = _current_fact(
        conn, subject_id=entity_id, predicate=predicate, fact_slot=fact_slot
    )
    new_ids = [str(item["id"]) for item in observations]
    new_sources = {OFFICIAL_WEB_SOURCE_ID}
    created = replaced = supports_created = 0

    if current is None:
        status = "single_source"
        fact_id = _opaque_id("fact", entity_id, predicate, fact_slot, value_hash, observed_at)
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
        for observation_id in new_ids:
            conn.execute(
                "INSERT INTO fact_observation_support(fact_id,observation_id,support_role) VALUES (?,?,'supports')",
                (fact_id, observation_id),
            )
            supports_created += 1
        return 1, 0, supports_created

    previous_supports = _usable_supports(conn, str(current["id"]))
    same_value = current["value_json"] == value_json and current["value_hash"] == value_hash
    previous_sources = {item["source_id"] for item in previous_supports}

    if str(observed_at) < str(current["valid_from"]):
        for observation_id in new_ids:
            if current["status"] == "confirmed" or new_sources <= previous_sources:
                conn.execute(
                    "INSERT OR IGNORE INTO fact_observation_support(fact_id,observation_id,support_role) "
                    "VALUES (?,?,'supports')",
                    (current["id"], observation_id),
                )
                supports_created += int(conn.execute("SELECT changes()").fetchone()[0] == 1)
        return 0, 0, supports_created

    if same_value and current["status"] == "confirmed":
        for observation_id in new_ids:
            conn.execute(
                "INSERT OR IGNORE INTO fact_observation_support(fact_id,observation_id,support_role) "
                "VALUES (?,?,'supports')",
                (current["id"], observation_id),
            )
            supports_created += int(conn.execute("SELECT changes()").fetchone()[0] == 1)
        return 0, 0, supports_created

    if same_value and new_sources <= previous_sources and str(observed_at) <= str(current["last_verified_at"] or current["valid_from"]):
        for observation_id in new_ids:
            conn.execute(
                "INSERT OR IGNORE INTO fact_observation_support(fact_id,observation_id,support_role) "
                "VALUES (?,?,'supports')",
                (current["id"], observation_id),
            )
            supports_created += int(conn.execute("SELECT changes()").fetchone()[0] == 1)
        return 0, 0, supports_created

    _close_fact(conn, str(current["id"]), observed_at)
    fact_id = _opaque_id("fact", entity_id, predicate, fact_slot, value_hash, observed_at)
    supporting_old: list[str] = []
    contradicting_old: list[str] = []
    if same_value:
        supporting_old = [item["observation_id"] for item in previous_supports]
        status = "confirmed" if len(previous_sources | new_sources) >= 2 else "single_source"
    else:
        contradicting_old = [item["observation_id"] for item in previous_supports]
        status = "single_source"
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
    for observation_id in [*supporting_old, *new_ids]:
        conn.execute(
            "INSERT OR IGNORE INTO fact_observation_support(fact_id,observation_id,support_role) "
            "VALUES (?,?,'supports')",
            (fact_id, observation_id),
        )
        supports_created += int(conn.execute("SELECT changes()").fetchone()[0] == 1)
    for observation_id in contradicting_old:
        conn.execute(
            "INSERT OR IGNORE INTO fact_observation_support(fact_id,observation_id,support_role) "
            "VALUES (?,?,'contradicts')",
            (fact_id, observation_id),
        )
        supports_created += int(conn.execute("SELECT changes()").fetchone()[0] == 1)
    return 1, 1, supports_created


def _reconcile_not_observed(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    predicate: str,
    session_id: str,
    observed_at: str,
) -> tuple[int, int]:
    current = _current_fact(
        conn, subject_id=entity_id, predicate=predicate, fact_slot="__single__"
    )
    if current is not None:
        current_value = None
        if current["value_json"] is not None:
            current_value = _parse_json_value(current["value_json"], label=f"current {predicate} fact")
        if current["status"] in {"confirmed", "single_source", "conflicted"} and current_value is not None:
            return 0, 0
        if current["status"] == "stale" and current_value is not None:
            return 0, 0
        if str(observed_at) < str(current["valid_from"]):
            return 0, 0
        _close_fact(conn, str(current["id"]), observed_at)

    fact_id = _opaque_id("fact", entity_id, predicate, "__single__", "not_observed", observed_at)
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
    return _opaque_id("ev", session_id, capture.final_url, capture.content_sha256)


def _finalize_failed_session(
    conn: sqlite3.Connection, *, session_id: str, status: str, finished_at: str, error: str
) -> None:
    if status not in {"blocked", "failed", "cancelled"}:
        raise ValueError("invalid terminal failure session status")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE acquisition_sessions SET status=?,finished_at=?,error=? "
            "WHERE id=? AND status='running'",
            (status, finished_at, error[:4000], session_id),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def collect_official_website(
    conn: sqlite3.Connection,
    *,
    evidence_root: str | Path,
    business_id: int | None = None,
    canonical_key: str | None = None,
    entity_id: str | None = None,
    config: CrawlConfig | None = None,
    now: Callable[[], str] = _utc_now,
    client_factory: Callable[..., SafeHttpClient] = SafeHttpClient,
) -> WebsiteAcquisitionStats:
    if sum(value is not None for value in (business_id, canonical_key, entity_id)) != 1:
        raise WebsiteAcquisitionError(
            "select exactly one of business_id, canonical_key, or entity_id"
        )
    if conn.in_transaction:
        raise WebsiteAcquisitionError(
            "website acquisition requires a connection with no active transaction"
        )
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise WebsiteAcquisitionError(
            "SQLite foreign-key enforcement must be enabled for website acquisition"
        )
    config = config or CrawlConfig()
    config.validate()
    root = Path(evidence_root)

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
    start_url, _website_fact = _official_website_fact(conn, resolved_entity)

    started_at = now()
    config_payload = {
        "entity_id": resolved_entity,
        "start_url": start_url,
        "page_limit": config.page_limit,
        "depth_limit": config.depth_limit,
        "max_response_bytes": config.max_response_bytes,
        "timeout_seconds": config.timeout_seconds,
        "user_agent": config.user_agent,
        "obey_robots": config.obey_robots,
        "evidence_root": str(root),
    }
    config_json = _canonical_json(config_payload)
    config_hash = _sha256_text(config_json)
    session_id = _opaque_id("acq", resolved_entity, started_at, config_hash)

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
                resolved_entity,
                OFFICIAL_WEB_SOURCE_ID,
                COLLECTOR_NAME,
                COLLECTOR_VERSION,
                config_json,
                config_hash,
                started_at,
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    client = client_factory(
        site_url=start_url,
        user_agent=config.user_agent,
        timeout_seconds=config.timeout_seconds,
        max_response_bytes=config.max_response_bytes,
        obey_robots=config.obey_robots,
    )
    try:
        captures, fetch_errors, canonical_home = _crawl(
            entity_id=resolved_entity,
            session_id=session_id,
            start_url=start_url,
            evidence_root=root,
            config=config,
            client=client,
            now=now,
        )
    except BaseException as exc:
        finished_at = now()
        _finalize_failed_session(
            conn,
            session_id=session_id,
            status="failed",
            finished_at=finished_at,
            error=str(exc),
        )
        raise

    if not captures:
        finished_at = now()
        terminal = "blocked" if fetch_errors and all("blocked" in item or "robots" in item for item in fetch_errors) else "failed"
        message = "; ".join(fetch_errors) or "website acquisition produced no usable pages"
        _finalize_failed_session(
            conn,
            session_id=session_id,
            status=terminal,
            finished_at=finished_at,
            error=message,
        )
        raise WebsiteAcquisitionError(message)

    finished_at = now()
    terminal_status = "partial" if fetch_errors else "complete"
    evidence_created = observations_created = channels_created = channels_refreshed = 0
    facts_created = facts_replaced = supports_created = absence_created = 0
    observed_predicates: set[str] = set()

    try:
        conn.execute("BEGIN IMMEDIATE")
        # Channels are deterministic entity-wide endpoints for the initial Phase-6 slice.
        # Each evidence item's metadata below records exactly which channel IDs it yielded.
        all_channels: dict[tuple[str, str], ChannelCandidate] = {}
        if canonical_home is not None:
            website = _website_channel(canonical_home)
            all_channels[(website.channel_type, website.normalized_identifier)] = website
        for capture in captures:
            for channel in capture.parsed.channels:
                all_channels[(channel.channel_type, channel.normalized_identifier)] = channel

        channel_ids: dict[tuple[str, str], str] = {}
        for key, channel in sorted(all_channels.items()):
            channel_id, created, refreshed = _ensure_channel(
                conn,
                entity_id=resolved_entity,
                channel=channel,
                observed_at=finished_at,
            )
            channel_ids[key] = channel_id
            channels_created += int(created)
            channels_refreshed += int(refreshed)

        observation_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for index, capture in enumerate(captures):
            evidence_id = _evidence_id(session_id, capture)
            page_channels = []
            for channel in capture.parsed.channels:
                key = (channel.channel_type, channel.normalized_identifier)
                page_channels.append(
                    {
                        "channel_id": channel_ids[key],
                        "channel_type": channel.channel_type,
                        "identifier": channel.identifier,
                        "normalized_identifier": channel.normalized_identifier,
                        "url": channel.url,
                        "extraction": channel.extraction,
                    }
                )
            if index == 0 and canonical_home is not None:
                website = _website_channel(canonical_home)
                key = (website.channel_type, website.normalized_identifier)
                page_channels.append(
                    {
                        "channel_id": channel_ids[key],
                        "channel_type": website.channel_type,
                        "identifier": website.identifier,
                        "normalized_identifier": website.normalized_identifier,
                        "url": website.url,
                        "extraction": website.extraction,
                    }
                )

            observation_specs = _page_observations(
                entity_id=resolved_entity,
                evidence_id=evidence_id,
                capture=capture,
                canonical_home=canonical_home,
                first_page=index == 0,
            )
            metadata_json = _canonical_json(
                {
                    "acquisition_kind": "bounded_official_website",
                    "entity_id": resolved_entity,
                    "requested_url": capture.requested_url,
                    "final_url": capture.final_url,
                    "crawl_depth": capture.depth,
                    "page_role": _page_role(capture.final_url),
                    "title": capture.parsed.title,
                    "canonical_url": capture.parsed.canonical_url,
                    "channels": sorted(page_channels, key=lambda item: item["channel_id"]),
                    "observation_ids": sorted(str(item["id"]) for item in observation_specs),
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
            for spec in observation_specs:
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
                observation_groups.setdefault(
                    (str(spec["predicate"]), str(spec["fact_slot"])), []
                ).append(spec)

        for (predicate, fact_slot), group in sorted(observation_groups.items()):
            group_observed_at = max(
                str(
                    _fetch_one(
                        conn,
                        "SELECT observed_at FROM observations WHERE id=?",
                        (item["id"],),
                    )["observed_at"]
                )
                for item in group
            )
            created, replaced, supports = _reconcile_observation_group(
                conn,
                entity_id=resolved_entity,
                predicate=predicate,
                fact_slot=fact_slot,
                observations=group,
                observed_at=group_observed_at,
                reconciled_at=finished_at,
            )
            facts_created += created
            facts_replaced += replaced
            supports_created += supports

        if terminal_status == "complete":
            for predicate in _CAPABILITY_PREDICATES:
                if predicate in observed_predicates:
                    continue
                created, support = _reconcile_not_observed(
                    conn,
                    entity_id=resolved_entity,
                    predicate=predicate,
                    session_id=session_id,
                    observed_at=finished_at,
                )
                absence_created += created
                supports_created += support

        error_text = None if not fetch_errors else "; ".join(fetch_errors)[:4000]
        conn.execute(
            "UPDATE acquisition_sessions SET status=?,finished_at=?,error=?,evidence_count=?,"
            "observation_count=? WHERE id=? AND status='running'",
            (
                terminal_status,
                finished_at,
                error_text,
                evidence_created,
                observations_created,
                session_id,
            ),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise WebsiteAcquisitionError("website acquisition session changed during finalization")
        violations = list(conn.execute("PRAGMA foreign_key_check"))
        if violations:
            raise WebsiteAcquisitionError(
                f"foreign-key violations after website acquisition: {violations!r}"
            )
        conn.commit()
    except BaseException as exc:
        conn.rollback()
        try:
            _finalize_failed_session(
                conn,
                session_id=session_id,
                status="failed",
                finished_at=now(),
                error=f"ingestion failed: {exc}",
            )
        except BaseException:
            pass
        raise

    unresolved = tuple(
        predicate
        for predicate in (
            "business.offering.service",
            "business.customer_segment.stated",
        )
        if predicate not in observed_predicates
    )
    return WebsiteAcquisitionStats(
        session_id=session_id,
        business_entity_id=resolved_entity,
        start_url=start_url,
        canonical_home_url=canonical_home,
        status=terminal_status,
        pages_fetched=len(captures),
        evidence_items_created=evidence_created,
        observations_created=observations_created,
        channels_created=channels_created,
        channels_refreshed=channels_refreshed,
        facts_created=facts_created,
        facts_replaced=facts_replaced,
        fact_support_links_created=supports_created,
        not_observed_facts_created=absence_created,
        fetch_errors=tuple(fetch_errors),
        unresolved_predicates=unresolved,
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sara-website",
        description="Run a bounded official-website acquisition into Business Understanding.",
    )
    parser.add_argument("--db", default="data/sara.db", help="SQLite database path")
    parser.add_argument(
        "--evidence-dir", default="evidence/web", help="directory for retained raw website HTML"
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--business-id", type=_positive_int)
    selector.add_argument("--canonical-key")
    selector.add_argument("--entity-id")
    parser.add_argument("--page-limit", type=_positive_int, default=8)
    parser.add_argument("--depth-limit", type=int, default=2)
    parser.add_argument("--max-response-bytes", type=_positive_int, default=1_048_576)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--user-agent", default="SaraBusinessUnderstanding/1.0")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_existing(Path(args.db))
        apply_migrations(conn)
        seed_business_understanding_vocabulary(conn)
        stats = collect_official_website(
            conn,
            evidence_root=Path(args.evidence_dir),
            business_id=args.business_id,
            canonical_key=args.canonical_key.strip() if args.canonical_key else None,
            entity_id=args.entity_id.strip() if args.entity_id else None,
            config=CrawlConfig(
                page_limit=args.page_limit,
                depth_limit=args.depth_limit,
                max_response_bytes=args.max_response_bytes,
                timeout_seconds=args.timeout,
                user_agent=args.user_agent,
            ),
        )
        kwargs = {"ensure_ascii": False, "sort_keys": True}
        payload = asdict(stats)
        if args.pretty:
            print(json.dumps(payload, indent=2, **kwargs))
        else:
            print(json.dumps(payload, separators=(",", ":"), **kwargs))
        return 0 if stats.status == "complete" else 3
    except KeyboardInterrupt:
        print("website acquisition interrupted", file=sys.stderr)
        return 130
    except (
        FileNotFoundError,
        MigrationError,
        VocabularySeedError,
        WebsiteAcquisitionError,
        WebsiteBlockedError,
        WebsiteFetchError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()
