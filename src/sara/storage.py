from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import BoundingBox


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    area_name TEXT NOT NULL,
    bbox_json TEXT NOT NULL,
    cell_km REAL NOT NULL,
    depth INTEGER NOT NULL,
    queries_json TEXT NOT NULL,
    scraper_image TEXT NOT NULL,
    config_json TEXT,
    raw_path TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    error TEXT,
    raw_records INTEGER NOT NULL DEFAULT 0,
    accepted_records INTEGER NOT NULL DEFAULT 0,
    out_of_bounds_records INTEGER NOT NULL DEFAULT 0,
    unlocated_records INTEGER NOT NULL DEFAULT 0,
    unidentified_records INTEGER NOT NULL DEFAULT 0,
    unique_seen INTEGER NOT NULL DEFAULT 0,
    new_businesses INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL UNIQUE,
    place_id TEXT,
    cid TEXT,
    data_id TEXT,
    title TEXT,
    category TEXT,
    address TEXT,
    latitude REAL,
    longitude REAL,
    phone TEXT,
    website TEXT,
    review_rating REAL,
    review_count INTEGER,
    status TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    first_run_id TEXT,
    last_run_id TEXT,
    raw_json TEXT NOT NULL,
    FOREIGN KEY(first_run_id) REFERENCES runs(id),
    FOREIGN KEY(last_run_id) REFERENCES runs(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_business_place_id
ON businesses(place_id) WHERE place_id IS NOT NULL AND place_id <> '';
CREATE UNIQUE INDEX IF NOT EXISTS ux_business_cid
ON businesses(cid) WHERE cid IS NOT NULL AND cid <> '';
CREATE UNIQUE INDEX IF NOT EXISTS ux_business_data_id
ON businesses(data_id) WHERE data_id IS NOT NULL AND data_id <> '';

CREATE TABLE IF NOT EXISTS run_businesses (
    run_id TEXT NOT NULL,
    business_id INTEGER NOT NULL,
    first_observed_at TEXT NOT NULL,
    PRIMARY KEY(run_id, business_id),
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE
);
"""

_RUN_COLUMN_MIGRATIONS = {
    "config_json": "config_json TEXT",
    "error": "error TEXT",
    "accepted_records": "accepted_records INTEGER NOT NULL DEFAULT 0",
    "out_of_bounds_records": "out_of_bounds_records INTEGER NOT NULL DEFAULT 0",
    "unlocated_records": "unlocated_records INTEGER NOT NULL DEFAULT 0",
    "unidentified_records": "unidentified_records INTEGER NOT NULL DEFAULT 0",
}


@dataclass(frozen=True)
class IngestStats:
    raw_records: int
    accepted_records: int
    out_of_bounds_records: int
    unlocated_records: int
    unidentified_records: int
    unique_seen: int
    new_businesses: int


class UnidentifiableRecord(ValueError):
    pass


class IdentityConflict(ValueError):
    pass


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    for name, definition in _RUN_COLUMN_MIGRATIONS.items():
        if name not in existing_columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {definition}")
    conn.commit()
    return conn


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    value = str(value).strip()
    return value or None


def _float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return None


def _coordinates(record: dict[str, Any]) -> tuple[float | None, float | None]:
    longitude = record.get("longitude")
    if longitude is None:
        longitude = record.get("longtitude")
    return _float(record.get("latitude")), _float(longitude)


def _identity(record: dict[str, Any]) -> tuple[str | None, str | None, str | None, str]:
    place_id = _text(record.get("place_id"))
    cid = _text(record.get("cid"))
    data_id = _text(record.get("data_id"))
    if place_id:
        return place_id, cid, data_id, f"place:{place_id}"
    if cid:
        return place_id, cid, data_id, f"cid:{cid}"
    if data_id:
        return place_id, cid, data_id, f"data:{data_id}"

    latitude, longitude = _coordinates(record)
    values = (
        _text(record.get("link")),
        _text(record.get("title")),
        _text(record.get("address")),
        _text(record.get("phone")),
        _text(record.get("website")),
        None if latitude is None else str(latitude),
        None if longitude is None else str(longitude),
    )
    normalized = [value.strip().lower() if value else "" for value in values]
    if not any(normalized):
        raise UnidentifiableRecord("record has no usable identity fields")
    digest = hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest()
    return place_id, cid, data_id, f"fallback:{digest}"


def _order_key(seen_at: str | None, run_id: str | None) -> tuple[str, str]:
    return str(seen_at or ""), str(run_id or "")


def _run_started_at(conn: sqlite3.Connection, run_id: str) -> str:
    row = conn.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"run_id {run_id} does not exist")
    return str(row["started_at"])


def _find_matches(
    conn: sqlite3.Connection,
    place_id: str | None,
    cid: str | None,
    data_id: str | None,
    canonical_key: str,
) -> list[sqlite3.Row]:
    matches: dict[int, sqlite3.Row] = {}
    for column, value in (("place_id", place_id), ("cid", cid), ("data_id", data_id), ("canonical_key", canonical_key)):
        if value:
            row = conn.execute(f"SELECT * FROM businesses WHERE {column} = ?", (value,)).fetchone()
            if row:
                matches[int(row["id"])] = row
    return [matches[key] for key in sorted(matches)]


def _merge_matches(conn: sqlite3.Connection, matches: list[sqlite3.Row]) -> sqlite3.Row | None:
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    primary = matches[0]
    identity_fields = ("place_id", "cid", "data_id")
    mutable_fields = (
        "title", "category", "address", "latitude", "longitude", "phone", "website",
        "review_rating", "review_count", "status",
    )
    earliest = min(matches, key=lambda row: _order_key(row["first_seen_at"], row["first_run_id"]))
    latest = max(matches, key=lambda row: _order_key(row["last_seen_at"], row["last_run_id"]))

    merged_identity = {field: primary[field] for field in identity_fields}
    for row in matches[1:]:
        for field in identity_fields:
            if merged_identity[field] in (None, "") and row[field] not in (None, ""):
                merged_identity[field] = row[field]

    merged_mutable = {field: latest[field] for field in mutable_fields}
    for row in sorted(matches, key=lambda item: _order_key(item["last_seen_at"], item["last_run_id"]), reverse=True):
        for field in mutable_fields:
            if merged_mutable[field] in (None, "") and row[field] not in (None, ""):
                merged_mutable[field] = row[field]

    for duplicate in matches[1:]:
        conn.execute(
            """
            INSERT OR IGNORE INTO run_businesses(run_id, business_id, first_observed_at)
            SELECT run_id, ?, first_observed_at FROM run_businesses WHERE business_id = ?
            """,
            (primary["id"], duplicate["id"]),
        )
        conn.execute("DELETE FROM businesses WHERE id = ?", (duplicate["id"],))

    conn.execute(
        """
        UPDATE businesses SET
            place_id = ?, cid = ?, data_id = ?, title = ?, category = ?, address = ?,
            latitude = ?, longitude = ?, phone = ?, website = ?, review_rating = ?,
            review_count = ?, status = ?, first_seen_at = ?, last_seen_at = ?,
            first_run_id = ?, last_run_id = ?, raw_json = ?
        WHERE id = ?
        """,
        (
            merged_identity["place_id"], merged_identity["cid"], merged_identity["data_id"],
            merged_mutable["title"], merged_mutable["category"], merged_mutable["address"],
            merged_mutable["latitude"], merged_mutable["longitude"], merged_mutable["phone"],
            merged_mutable["website"], merged_mutable["review_rating"], merged_mutable["review_count"],
            merged_mutable["status"], earliest["first_seen_at"], latest["last_seen_at"],
            earliest["first_run_id"], latest["last_run_id"], latest["raw_json"], primary["id"],
        ),
    )
    return conn.execute("SELECT * FROM businesses WHERE id = ?", (primary["id"],)).fetchone()


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    latitude, longitude = _coordinates(record)
    return {
        "title": _text(record.get("title")),
        "category": _text(record.get("category")),
        "address": _text(record.get("address")),
        "latitude": latitude,
        "longitude": longitude,
        "phone": _text(record.get("phone")),
        "website": _text(record.get("website")),
        "review_rating": _float(record.get("review_rating")),
        "review_count": _int(record.get("review_count")),
        "status": _text(record.get("status")),
        "raw_json": json.dumps(record, ensure_ascii=False, sort_keys=True),
    }


def _assert_identity_compatible(
    existing: sqlite3.Row,
    place_id: str | None,
    cid: str | None,
    data_id: str | None,
) -> None:
    for field, incoming in (("place_id", place_id), ("cid", cid), ("data_id", data_id)):
        current = existing[field]
        if current not in (None, "") and incoming not in (None, "") and current != incoming:
            raise IdentityConflict(
                f"strong identity conflict for business {existing['id']}: {field}={current!r} vs {incoming!r}"
            )


def _prefer(incoming: Any, current: Any, *, incoming_is_latest: bool) -> Any:
    if incoming_is_latest:
        return incoming if incoming not in (None, "") else current
    return current if current not in (None, "") else incoming


def upsert_business(
    conn: sqlite3.Connection,
    run_id: str,
    record: dict[str, Any],
    *,
    run_started_at: str | None = None,
) -> tuple[int, bool]:
    place_id, cid, data_id, canonical_key = _identity(record)
    matches = _find_matches(conn, place_id, cid, data_id, canonical_key)
    existing = _merge_matches(conn, matches)
    observed_at = run_started_at or _run_started_at(conn, run_id)
    payload = _payload(record)

    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO businesses (
                canonical_key, place_id, cid, data_id, title, category, address,
                latitude, longitude, phone, website, review_rating, review_count,
                status, first_seen_at, last_seen_at, first_run_id, last_run_id, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical_key, place_id, cid, data_id, payload["title"], payload["category"], payload["address"],
                payload["latitude"], payload["longitude"], payload["phone"], payload["website"],
                payload["review_rating"], payload["review_count"], payload["status"],
                observed_at, observed_at, run_id, run_id, payload["raw_json"],
            ),
        )
        return int(cursor.lastrowid), True

    _assert_identity_compatible(existing, place_id, cid, data_id)
    incoming_key = _order_key(observed_at, run_id)
    first_key = _order_key(existing["first_seen_at"], existing["first_run_id"])
    last_key = _order_key(existing["last_seen_at"], existing["last_run_id"])
    incoming_is_latest = incoming_key >= last_key

    first_seen_at = observed_at if incoming_key < first_key else existing["first_seen_at"]
    first_run_id = run_id if incoming_key < first_key else existing["first_run_id"]
    last_seen_at = observed_at if incoming_is_latest else existing["last_seen_at"]
    last_run_id = run_id if incoming_is_latest else existing["last_run_id"]

    mutable_fields = (
        "title", "category", "address", "latitude", "longitude", "phone", "website",
        "review_rating", "review_count", "status",
    )
    merged_payload = {
        field: _prefer(payload[field], existing[field], incoming_is_latest=incoming_is_latest)
        for field in mutable_fields
    }
    raw_json = payload["raw_json"] if incoming_is_latest else existing["raw_json"]

    conn.execute(
        """
        UPDATE businesses SET
            place_id = ?, cid = ?, data_id = ?,
            title = ?, category = ?, address = ?, latitude = ?, longitude = ?, phone = ?,
            website = ?, review_rating = ?, review_count = ?, status = ?,
            first_seen_at = ?, last_seen_at = ?, first_run_id = ?, last_run_id = ?, raw_json = ?
        WHERE id = ?
        """,
        (
            existing["place_id"] or place_id,
            existing["cid"] or cid,
            existing["data_id"] or data_id,
            merged_payload["title"], merged_payload["category"], merged_payload["address"],
            merged_payload["latitude"], merged_payload["longitude"], merged_payload["phone"],
            merged_payload["website"], merged_payload["review_rating"], merged_payload["review_count"],
            merged_payload["status"], first_seen_at, last_seen_at, first_run_id, last_run_id,
            raw_json, existing["id"],
        ),
    )
    return int(existing["id"]), False


def _refresh_canonical_counts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE runs
        SET unique_seen = (
                SELECT COUNT(*) FROM run_businesses rb WHERE rb.run_id = runs.id
            ),
            new_businesses = (
                SELECT COUNT(*) FROM businesses b WHERE b.first_run_id = runs.id
            )
        """
    )


def ingest_records(
    conn: sqlite3.Connection,
    run_id: str,
    records: Iterable[dict[str, Any]],
    *,
    bbox: BoundingBox | None = None,
    finalize_run: tuple[str, int | None, str | None] | None = None,
) -> IngestStats:
    run = conn.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"run_id {run_id} does not exist")
    run_started_at = str(run["started_at"])

    raw_records = 0
    accepted_records = 0
    out_of_bounds_records = 0
    unlocated_records = 0
    unidentified_records = 0

    try:
        conn.execute("BEGIN IMMEDIATE")
        for record in records:
            raw_records += 1
            if bbox is not None:
                latitude, longitude = _coordinates(record)
                if latitude is None or longitude is None:
                    unlocated_records += 1
                    continue
                if not bbox.contains(latitude, longitude):
                    out_of_bounds_records += 1
                    continue

            try:
                business_id, _ = upsert_business(
                    conn,
                    run_id,
                    record,
                    run_started_at=run_started_at,
                )
            except UnidentifiableRecord:
                unidentified_records += 1
                continue

            accepted_records += 1
            conn.execute(
                "INSERT OR IGNORE INTO run_businesses(run_id, business_id, first_observed_at) VALUES (?, ?, ?)",
                (run_id, business_id, run_started_at),
            )

        conn.execute(
            """
            UPDATE runs SET
                raw_records = ?, accepted_records = ?, out_of_bounds_records = ?,
                unlocated_records = ?, unidentified_records = ?
            WHERE id = ?
            """,
            (
                raw_records, accepted_records, out_of_bounds_records,
                unlocated_records, unidentified_records, run_id,
            ),
        )
        _refresh_canonical_counts(conn)

        if finalize_run is not None:
            status, exit_code, error = finalize_run
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ?, exit_code = ?, error = ? WHERE id = ?",
                (status, utc_now(), exit_code, error, run_id),
            )

        row = conn.execute(
            """
            SELECT raw_records, accepted_records, out_of_bounds_records, unlocated_records,
                   unidentified_records, unique_seen, new_businesses
            FROM runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        stats = IngestStats(
            raw_records=int(row["raw_records"]),
            accepted_records=int(row["accepted_records"]),
            out_of_bounds_records=int(row["out_of_bounds_records"]),
            unlocated_records=int(row["unlocated_records"]),
            unidentified_records=int(row["unidentified_records"]),
            unique_seen=int(row["unique_seen"]),
            new_businesses=int(row["new_businesses"]),
        )
        conn.commit()
        return stats
    except BaseException:
        conn.rollback()
        raise


def iter_jsonl(path: str | Path):
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"expected JSON object on line {line_number}")
            yield value
