from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    area_name TEXT NOT NULL,
    bbox_json TEXT NOT NULL,
    cell_km REAL NOT NULL,
    depth INTEGER NOT NULL,
    queries_json TEXT NOT NULL,
    scraper_image TEXT NOT NULL,
    raw_path TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    raw_records INTEGER NOT NULL DEFAULT 0,
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


@dataclass(frozen=True)
class IngestStats:
    raw_records: int
    unique_seen: int
    new_businesses: int


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
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

    fallback = "|".join(
        str(record.get(key) or "").strip().lower()
        for key in ("title", "phone", "website", "latitude", "longitude")
    )
    digest = hashlib.sha256(fallback.encode("utf-8")).hexdigest()
    return place_id, cid, data_id, f"fallback:{digest}"


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
    primary = matches[0]
    if len(matches) == 1:
        return primary

    fields = (
        "place_id", "cid", "data_id", "title", "category", "address", "latitude",
        "longitude", "phone", "website", "review_rating", "review_count", "status",
    )
    merged = {field: primary[field] for field in fields}
    first_seen_at = primary["first_seen_at"]
    first_run_id = primary["first_run_id"]

    for duplicate in matches[1:]:
        for field in fields:
            if merged[field] in (None, "") and duplicate[field] not in (None, ""):
                merged[field] = duplicate[field]
        if duplicate["first_seen_at"] < first_seen_at:
            first_seen_at = duplicate["first_seen_at"]
            first_run_id = duplicate["first_run_id"]
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
            review_count = ?, status = ?, first_seen_at = ?, first_run_id = ?
        WHERE id = ?
        """,
        tuple(merged[field] for field in fields) + (first_seen_at, first_run_id, primary["id"]),
    )
    return conn.execute("SELECT * FROM businesses WHERE id = ?", (primary["id"],)).fetchone()


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    longitude = record.get("longitude")
    if longitude is None:
        longitude = record.get("longtitude")
    return {
        "title": _text(record.get("title")),
        "category": _text(record.get("category")),
        "address": _text(record.get("address")),
        "latitude": _float(record.get("latitude")),
        "longitude": _float(longitude),
        "phone": _text(record.get("phone")),
        "website": _text(record.get("website")),
        "review_rating": _float(record.get("review_rating")),
        "review_count": _int(record.get("review_count")),
        "status": _text(record.get("status")),
        "raw_json": json.dumps(record, ensure_ascii=False, sort_keys=True),
    }


def upsert_business(conn: sqlite3.Connection, run_id: str, record: dict[str, Any]) -> tuple[int, bool]:
    place_id, cid, data_id, canonical_key = _identity(record)
    matches = _find_matches(conn, place_id, cid, data_id, canonical_key)
    existing = _merge_matches(conn, matches)
    now = utc_now()
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
                payload["review_rating"], payload["review_count"], payload["status"], now, now, run_id, run_id,
                payload["raw_json"],
            ),
        )
        return int(cursor.lastrowid), True

    conn.execute(
        """
        UPDATE businesses SET
            place_id = COALESCE(?, place_id), cid = COALESCE(?, cid), data_id = COALESCE(?, data_id),
            title = COALESCE(?, title), category = COALESCE(?, category), address = COALESCE(?, address),
            latitude = COALESCE(?, latitude), longitude = COALESCE(?, longitude), phone = COALESCE(?, phone),
            website = COALESCE(?, website), review_rating = COALESCE(?, review_rating),
            review_count = COALESCE(?, review_count), status = COALESCE(?, status),
            last_seen_at = ?, last_run_id = ?, raw_json = ?
        WHERE id = ?
        """,
        (
            place_id, cid, data_id, payload["title"], payload["category"], payload["address"],
            payload["latitude"], payload["longitude"], payload["phone"], payload["website"],
            payload["review_rating"], payload["review_count"], payload["status"], now, run_id,
            payload["raw_json"], existing["id"],
        ),
    )
    return int(existing["id"]), False


def ingest_records(conn: sqlite3.Connection, run_id: str, records: Iterable[dict[str, Any]]) -> IngestStats:
    raw_records = 0
    new_businesses = 0
    seen_ids: set[int] = set()
    now = utc_now()

    for record in records:
        raw_records += 1
        business_id, is_new = upsert_business(conn, run_id, record)
        if is_new:
            new_businesses += 1
        seen_ids.add(business_id)
        conn.execute(
            "INSERT OR IGNORE INTO run_businesses(run_id, business_id, first_observed_at) VALUES (?, ?, ?)",
            (run_id, business_id, now),
        )

    conn.execute(
        "UPDATE runs SET raw_records = ?, unique_seen = ?, new_businesses = ? WHERE id = ?",
        (raw_records, len(seen_ids), new_businesses, run_id),
    )
    conn.commit()
    return IngestStats(raw_records, len(seen_ids), new_businesses)


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
