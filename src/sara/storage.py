from __future__ import annotations

import hashlib
import json
import math
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


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an existing Sara database strictly read-only.

    A nonexistent database is rejected, never created. No schema, migration
    or journal-mode statement runs here. Read-only is enforced at SQLite
    open time via a URI-escaped ``mode=ro`` connection plus
    ``PRAGMA query_only`` where supported.

    The guarantee is logical (no data/schema mutation); depending on
    platform, SQLite version and journal state the driver may still touch
    auxiliary lock/SHM files. ``immutable=1`` is deliberately not used.
    """
    db_path = Path(path)
    if not db_path.is_file():
        raise FileNotFoundError(f"database does not exist: {db_path}")
    # Path.as_uri() percent-encodes spaces, '?', '#', '%' and non-ASCII and
    # yields an absolute file: URI for POSIX and Windows drive layouts, so
    # appending the query parameter is unambiguous.
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        # mode=ro already enforces read-only at the driver level.
        pass
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


# ---------------------------------------------------------------------------
# Recovery execution storage (recovery-run)
# ---------------------------------------------------------------------------

RECOVERY_EXECUTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_executions (
    plan_sha256 TEXT PRIMARY KEY,
    source_run_id TEXT NOT NULL,
    plan_schema_version INTEGER NOT NULL,
    plan_kind TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    output_root TEXT NOT NULL,
    plan_snapshot_path TEXT NOT NULL,
    selected_bins INTEGER NOT NULL,
    planned_searches INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT,
    result_json TEXT,
    FOREIGN KEY(source_run_id) REFERENCES runs(id)
)
"""

RECOVERY_EXECUTION_BINS_SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_execution_bins (
    plan_sha256 TEXT NOT NULL,
    row INTEGER NOT NULL,
    column INTEGER NOT NULL,
    tier TEXT NOT NULL,
    bbox_json TEXT NOT NULL,
    planned_searches INTEGER NOT NULL,
    run_id TEXT UNIQUE,
    container_name TEXT NOT NULL UNIQUE,
    PRIMARY KEY(plan_sha256, row, column),
    FOREIGN KEY(plan_sha256) REFERENCES recovery_executions(plan_sha256)
        ON DELETE CASCADE,
    FOREIGN KEY(run_id) REFERENCES runs(id)
)
"""

_FAULT_HOOKS: dict[str, Any] = {}


class RecoverySchemaError(RuntimeError):
    """The existing recovery tables do not have the required v1 shape."""


class SourceRunRejected(ValueError):
    """Stable source-run provenance validation failed (caller rejection)."""


def set_recovery_fault_hook(name: str, hook: Any) -> None:
    """Install a fault-injection hook used only by tests."""
    _FAULT_HOOKS[name] = hook


def clear_recovery_fault_hook(name: str) -> None:
    _FAULT_HOOKS.pop(name, None)


def _run_fault_hook(name: str) -> None:
    hook = _FAULT_HOOKS.get(name)
    if hook is not None:
        hook()


def connect_existing(path: str | Path) -> sqlite3.Connection:
    """Open an existing Sara database write-capably without any schema work.

    Unlike :func:`connect`, this never runs migrations or DDL on open, so the
    caller controls when (and whether) recovery tables are created. Used by
    recovery-run after the read-only source preflight to close the TOCTOU
    window inside one ``BEGIN IMMEDIATE`` transaction.
    """
    db_path = Path(path)
    if not db_path.is_file():
        raise FileNotFoundError(f"database does not exist: {db_path}")
    # mode=rw makes SQLite itself refuse to create the file if it disappears
    # between the is_file() check and the open (a disappearance race would
    # otherwise silently produce an empty database).
    uri = f"{db_path.resolve().as_uri()}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def ensure_recovery_schema(conn: sqlite3.Connection) -> None:
    """Create the recovery tables if absent (transactional, no executescript)."""
    conn.execute(RECOVERY_EXECUTIONS_SCHEMA.strip())
    conn.execute(RECOVERY_EXECUTION_BINS_SCHEMA.strip())


def verify_recovery_schema(conn: sqlite3.Connection) -> None:
    """Verify the recovery tables have exactly the required v1 shape."""
    _verify_table_columns(
        conn, "recovery_executions",
        [
            ("plan_sha256", "TEXT", 1, False), ("source_run_id", "TEXT", 0, False),
            ("plan_schema_version", "INTEGER", 0, False), ("plan_kind", "TEXT", 0, False),
            ("policy_id", "TEXT", 0, False), ("output_root", "TEXT", 0, False),
            ("plan_snapshot_path", "TEXT", 0, False), ("selected_bins", "INTEGER", 0, False),
            ("planned_searches", "INTEGER", 0, False), ("status", "TEXT", 0, False),
            ("started_at", "TEXT", 0, False), ("finished_at", "TEXT", 0, True),
            ("error", "TEXT", 0, True), ("result_json", "TEXT", 0, True),
        ],
        expected_pk=["plan_sha256"],
    )
    _verify_table_columns(
        conn, "recovery_execution_bins",
        [
            ("plan_sha256", "TEXT", 1, False), ("row", "INTEGER", 2, False),
            ("column", "INTEGER", 3, False), ("tier", "TEXT", 0, False),
            ("bbox_json", "TEXT", 0, False), ("planned_searches", "INTEGER", 0, False),
            ("run_id", "TEXT", 0, True), ("container_name", "TEXT", 0, False),
        ],
        expected_pk=["plan_sha256", "row", "column"],
    )
    _verify_unique_index(conn, "recovery_execution_bins", "run_id")
    _verify_unique_index(conn, "recovery_execution_bins", "container_name")

    parent_fks = {
        (row["table"], row["from"], row["to"], row["on_delete"])
        for row in conn.execute("PRAGMA foreign_key_list(recovery_execution_bins)")
    }
    if ("recovery_executions", "plan_sha256", "plan_sha256", "CASCADE") not in parent_fks:
        raise RecoverySchemaError("recovery_execution_bins is missing the parent-plan foreign key")
    if ("runs", "run_id", "id", "NO ACTION") not in parent_fks:
        raise RecoverySchemaError("recovery_execution_bins is missing the child-run foreign key")
    source_fks = {
        (row["table"], row["from"], row["to"])
        for row in conn.execute("PRAGMA foreign_key_list(recovery_executions)")
    }
    if ("runs", "source_run_id", "id") not in source_fks:
        raise RecoverySchemaError("recovery_executions is missing the source-run foreign key")


def _verify_table_columns(conn, table, expected, *, expected_pk) -> None:
    rows = list(conn.execute(f"PRAGMA table_info({table})"))
    if not rows:
        raise RecoverySchemaError(f"table {table} does not exist")
    # expected rows carry (name, type, pk_ordinal); nullability is required
    # for every non-PK column (SQLite reports TEXT PRIMARY KEY columns as
    # nullable in table_info unless NOT NULL is declared, which is accepted
    # for PK columns whose uniqueness already implies presence).
    actual = [(row["name"], row["type"], row["pk"]) for row in rows]
    expected_layout = [(name, ctype, pk) for name, ctype, pk, _n in expected]
    if actual != expected_layout:
        raise RecoverySchemaError(
            f"table {table} has an incompatible column layout: {actual!r}"
        )
    pk_columns = [row["name"] for row in sorted((r for r in rows if r["pk"]), key=lambda r: r["pk"])]
    if pk_columns != expected_pk:
        raise RecoverySchemaError(f"table {table} primary key does not match: {pk_columns!r}")
    expected_by_name = {name: (ctype, pk, nullable) for name, ctype, pk, nullable in expected}
    for row in rows:
        ctype, pk, nullable = expected_by_name[row["name"]]
        if pk == 0 and not nullable and row["notnull"] != 1:
            raise RecoverySchemaError(
                f"table {table} column {row['name']} must be declared NOT NULL"
            )


def _verify_unique_index(conn, table, column) -> None:
    for index in conn.execute(f"PRAGMA index_list({table})"):
        if not index["unique"]:
            continue
        columns = [row["name"] for row in conn.execute(f"PRAGMA index_info({index['name']})")]
        if columns == [column]:
            return
    raise RecoverySchemaError(f"table {table} is missing the UNIQUE constraint on {column}")


_SOURCE_RUN_COLUMNS = (
    "id, area_name, bbox_json, cell_km, depth, queries_json, scraper_image, "
    "config_json, status, started_at, finished_at, exit_code, unique_seen"
)


def _storage_reject_constant(token: str) -> None:
    raise ValueError(f"non-standard JSON constant {token!r} in recorded run state")


def load_recovery_source_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Validate stable source-run provenance shared by planner and executor.

    Checks the run row exists, is complete with a finished_at timestamp, has
    strictly typed bbox/queries/config text, records resume=true and
    strict_bounds=true, and that the denormalized run columns agree with the
    recorded configuration. Historical config bytes are never normalized or
    rewritten; the config hash therefore stays meaningful.

    This deliberately does NOT inspect current business coordinates,
    last_run_id values, or run membership: those are planner-time
    current-state checks, not stable provenance.
    """
    row = conn.execute(
        f"SELECT {_SOURCE_RUN_COLUMNS} FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise SourceRunRejected(f"run_id {run_id} does not exist")
    if row["status"] != "complete":
        raise SourceRunRejected(f"run_id {run_id} is not complete (status={row['status']!r})")
    if not isinstance(row["finished_at"], str) or not row["finished_at"].strip():
        raise SourceRunRejected("complete run has an invalid finished_at timestamp")
    if not isinstance(row["started_at"], str) or not row["started_at"].strip():
        raise SourceRunRejected("run has an invalid started_at timestamp")
    if row["exit_code"] is not None and (
        isinstance(row["exit_code"], bool) or not isinstance(row["exit_code"], int)
    ):
        raise SourceRunRejected("run has an invalid recorded exit_code")

    if not isinstance(row["bbox_json"], str):
        raise SourceRunRejected("run bbox_json is not stored as text")
    try:
        bbox_raw = json.loads(row["bbox_json"], parse_constant=_storage_reject_constant)
    except (TypeError, ValueError) as exc:
        raise SourceRunRejected(f"run has invalid recorded bbox: {exc}") from exc
    if not isinstance(bbox_raw, dict):
        raise SourceRunRejected("run has invalid recorded bbox: expected a JSON object")
    bbox_keys = ("min_lat", "min_lon", "max_lat", "max_lon")
    for key in bbox_keys:
        if key not in bbox_raw:
            raise SourceRunRejected(f"run has invalid recorded bbox: missing {key}")
    for key, value in bbox_raw.items():
        if key not in bbox_keys:
            raise SourceRunRejected(f"run has invalid recorded bbox: unexpected key {key!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SourceRunRejected(f"run has invalid recorded bbox: {key} must be a number")
    try:
        bbox = BoundingBox(
            min_lat=bbox_raw["min_lat"], min_lon=bbox_raw["min_lon"],
            max_lat=bbox_raw["max_lat"], max_lon=bbox_raw["max_lon"],
        )
        bbox.validate()
    except ValueError as exc:
        raise SourceRunRejected(f"run has invalid recorded bbox: {exc}") from exc

    if not isinstance(row["queries_json"], str):
        raise SourceRunRejected("run queries_json is not stored as text")
    try:
        queries = json.loads(row["queries_json"], parse_constant=_storage_reject_constant)
    except (TypeError, ValueError) as exc:
        raise SourceRunRejected(f"run has invalid recorded queries: {exc}") from exc
    if (
        not isinstance(queries, list)
        or not queries
        or not all(isinstance(query, str) and query for query in queries)
    ):
        raise SourceRunRejected("run has invalid recorded query configuration")

    config_raw = row["config_json"]
    if not isinstance(config_raw, str) or not config_raw:
        raise SourceRunRejected("run has no recorded configuration text")
    try:
        config = json.loads(config_raw, parse_constant=_storage_reject_constant)
    except (TypeError, ValueError) as exc:
        raise SourceRunRejected(f"run has invalid recorded configuration: {exc}") from exc
    if not isinstance(config, dict):
        raise SourceRunRejected("run configuration is not a JSON object")
    if config.get("resume") is not True:
        raise SourceRunRejected("recovery planning requires a run recorded with resume=true")
    if config.get("strict_bounds") is not True:
        raise SourceRunRejected("recovery planning requires a run recorded with strict_bounds=true")

    source_cell_km = row["cell_km"]
    if (
        isinstance(source_cell_km, bool)
        or not isinstance(source_cell_km, (int, float))
        or not math.isfinite(source_cell_km)
        or source_cell_km <= 0
    ):
        raise SourceRunRejected("run has an invalid recorded cell size")

    def mismatch(field: str) -> SourceRunRejected:
        return SourceRunRejected(
            f"run {field} disagrees with the recorded configuration; "
            "the run row is internally inconsistent"
        )

    if not isinstance(config.get("area_name"), str) or config["area_name"] != row["area_name"]:
        raise mismatch("area_name")
    if not isinstance(config.get("image"), str) or config["image"] != row["scraper_image"]:
        raise mismatch("scraper_image")
    recorded_bbox = config.get("bbox")
    if not isinstance(recorded_bbox, dict):
        raise mismatch("bbox")
    for key, value in (
        ("min_lat", bbox.min_lat), ("min_lon", bbox.min_lon),
        ("max_lat", bbox.max_lat), ("max_lon", bbox.max_lon),
    ):
        recorded = recorded_bbox.get(key)
        if (
            isinstance(recorded, bool)
            or not isinstance(recorded, (int, float))
            or recorded != value
        ):
            raise mismatch("bbox")
    if config.get("queries") != queries:
        raise mismatch("queries")
    recorded_cell = config.get("cell_km")
    if (
        isinstance(recorded_cell, bool)
        or not isinstance(recorded_cell, (int, float))
        or recorded_cell != source_cell_km
    ):
        raise mismatch("cell_km")
    recorded_depth = config.get("depth")
    if (
        isinstance(recorded_depth, bool)
        or not isinstance(recorded_depth, int)
        or not isinstance(row["depth"], int)
        or recorded_depth != row["depth"]
    ):
        raise mismatch("depth")

    return {
        "row": row,
        "bbox": bbox,
        "queries": queries,
        "config": config,
        "config_raw": config_raw,
        "source_cell_km": source_cell_km,
        "config_sha256": hashlib.sha256(config_raw.encode("utf-8")).hexdigest(),
    }


def bind_plan_to_source(conn: sqlite3.Connection, plan) -> dict[str, Any]:
    """Recovery-run source binding: stable provenance only.

    Compares the plan's recorded source values against the current run row.
    Never inspects current canonical membership or coordinates.
    """
    from .recovery import project_source_config

    record = load_recovery_source_run(conn, plan.source_run_id)
    row = record["row"]
    expectations = (
        ("area_name", plan.area_name, row["area_name"]),
        ("cell_km", plan.cell_km, float(record["source_cell_km"])),
        ("depth", plan.depth, row["depth"]),
        ("queries", list(plan.queries), record["queries"]),
        ("scraper_image", plan.scraper_image, row["scraper_image"]),
        ("started_at", plan.started_at, row["started_at"]),
        ("finished_at", plan.finished_at, row["finished_at"]),
        ("exit_code", plan.exit_code, row["exit_code"]),
        ("config_sha256", plan.config_sha256, record["config_sha256"]),
        ("bbox", plan.bbox, record["bbox"]),
        ("config", plan.config, project_source_config(record["config"])),
    )
    for field, planned, current in expectations:
        if planned != current:
            raise SourceRunRejected(
                f"plan source_run.{field} does not match the current source run state; "
                "refusing to execute against this database"
            )
    return record


def register_recovery_execution(
    conn: sqlite3.Connection,
    *,
    plan,
    plan_sha256: str,
    output_root: str,
    plan_snapshot_path: str,
    container_names: dict[tuple[int, int], str],
) -> str:
    """Register or reconcile one recovery execution inside the caller's transaction.

    Returns "registered" for a fresh parent, "resumed" for a matching
    incomplete parent. Raises for immutable-metadata disagreement or an
    inconsistent registered mapping set. The caller owns BEGIN IMMEDIATE and
    COMMIT so registration is atomic with source revalidation and schema
    creation.
    """
    existing = conn.execute(
        "SELECT * FROM recovery_executions WHERE plan_sha256 = ?", (plan_sha256,)
    ).fetchone()
    if existing is not None:
        immutable = {
            "source_run_id": plan.source_run_id,
            "plan_schema_version": 1,
            "plan_kind": "sara.recovery_plan",
            "policy_id": plan.policy.policy_id,
            "output_root": output_root,
            "plan_snapshot_path": plan_snapshot_path,
            "selected_bins": len(plan.selected_bins),
            "planned_searches": plan.targeted_searches,
        }
        for field, expected in immutable.items():
            if existing[field] != expected:
                raise RecoverySchemaError(
                    f"existing recovery execution for this plan hash has a different {field}; "
                    "refusing to resume an inconsistent execution"
                )
        if existing["status"] == "complete":
            return "complete"
        registered = list(conn.execute(
            "SELECT row, column, tier, bbox_json, planned_searches, run_id, container_name "
            "FROM recovery_execution_bins WHERE plan_sha256 = ? ORDER BY row, column",
            (plan_sha256,),
        ))
        expected_bins = sorted(plan.selected_bins, key=lambda b: (b.row, b.column))
        if len(registered) != len(expected_bins):
            raise RecoverySchemaError(
                "existing recovery execution has an inconsistent registered bin set"
            )
        for recorded, expected in zip(registered, expected_bins):
            if (
                recorded["row"] != expected.row
                or recorded["column"] != expected.column
                or recorded["tier"] != expected.tier
                or recorded["planned_searches"] != (expected.planned_searches or 0)
                or recorded["container_name"] != container_names[(expected.row, expected.column)]
            ):
                raise RecoverySchemaError(
                    "existing recovery execution bin mapping does not match the plan"
                )
        return "resumed"

    conn.execute(
        """
        INSERT INTO recovery_executions(
            plan_sha256, source_run_id, plan_schema_version, plan_kind, policy_id,
            output_root, plan_snapshot_path, selected_bins, planned_searches,
            status, started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
        """,
        (
            plan_sha256, plan.source_run_id, 1, "sara.recovery_plan",
            plan.policy.policy_id, output_root, plan_snapshot_path,
            len(plan.selected_bins), plan.targeted_searches, utc_now(),
        ),
    )
    _run_fault_hook("after_parent_insert")
    for bin_record in sorted(plan.selected_bins, key=lambda b: (b.row, b.column)):
        conn.execute(
            """
            INSERT INTO recovery_execution_bins(
                plan_sha256, row, column, tier, bbox_json, planned_searches,
                run_id, container_name)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                plan_sha256, bin_record.row, bin_record.column, bin_record.tier,
                json.dumps({
                    "min_lat": bin_record.bbox.min_lat, "min_lon": bin_record.bbox.min_lon,
                    "max_lat": bin_record.bbox.max_lat, "max_lon": bin_record.bbox.max_lon,
                }, ensure_ascii=False, sort_keys=True),
                bin_record.planned_searches,
                container_names[(bin_record.row, bin_record.column)],
            ),
        )
    _run_fault_hook("after_mapping_inserts")

    registered = conn.execute(
        "SELECT COUNT(*) AS n FROM recovery_execution_bins WHERE plan_sha256 = ?",
        (plan_sha256,),
    ).fetchone()["n"]
    if registered != len(plan.selected_bins):
        raise RecoverySchemaError("registration validation failed: mapping set incomplete")
    return "registered"


def create_recovery_child_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    area_name: str,
    bbox_json: str,
    cell_km: float,
    depth: int,
    queries_json: str,
    scraper_image: str,
    config_json: str,
    raw_path: str,
    plan_sha256: str,
    row: int,
    column: int,
) -> None:
    """Create the child runs row and assign the mapping atomically.

    The caller owns the surrounding short transaction (BEGIN IMMEDIATE /
    COMMIT). started_at is the earliest possible acquisition-observation
    ordering proxy for this logical child, set at first actual start.
    """
    conn.execute(
        """
        INSERT INTO runs(
            id, area_name, bbox_json, cell_km, depth, queries_json, scraper_image,
            config_json, raw_path, status, started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
        """,
        (
            run_id, area_name, bbox_json, cell_km, depth, queries_json,
            scraper_image, config_json, raw_path, utc_now(),
        ),
    )
    _run_fault_hook("after_child_run_insert")
    updated = conn.execute(
        "UPDATE recovery_execution_bins SET run_id = ? "
        "WHERE plan_sha256 = ? AND row = ? AND column = ? AND run_id IS NULL",
        (run_id, plan_sha256, row, column),
    ).rowcount
    if updated != 1:
        raise RecoverySchemaError(
            "child run mapping assignment did not match exactly one unstarted bin"
        )


def set_recovery_parent_status(
    conn: sqlite3.Connection, plan_sha256: str, status: str, error: str | None
) -> None:
    conn.execute(
        "UPDATE recovery_executions SET status = ?, finished_at = ?, error = ? WHERE plan_sha256 = ?",
        (status, utc_now(), error, plan_sha256),
    )


def resume_recovery_parent(conn: sqlite3.Connection, plan_sha256: str) -> None:
    conn.execute(
        "UPDATE recovery_executions SET status = 'running', finished_at = NULL, error = NULL "
        "WHERE plan_sha256 = ?",
        (plan_sha256,),
    )


def compute_recovery_metrics(
    conn: sqlite3.Connection, plan_sha256: str, source_run_id: str, at_plan_count: int
) -> dict[str, int]:
    child_rows = list(conn.execute(
        "SELECT rb.run_id AS run_id FROM recovery_execution_bins rb "
        "WHERE rb.plan_sha256 = ? AND rb.run_id IS NOT NULL", (plan_sha256,)
    ))
    child_ids = [row["run_id"] for row in child_rows]
    placeholders = ",".join("?" for _ in child_ids) or "NULL"
    sums = conn.execute(
        f"""
        SELECT COALESCE(SUM(raw_records), 0) AS raw_records,
               COALESCE(SUM(accepted_records), 0) AS accepted_records,
               COALESCE(SUM(out_of_bounds_records), 0) AS out_of_bounds_records,
               COALESCE(SUM(unlocated_records), 0) AS unlocated_records,
               COALESCE(SUM(unidentified_records), 0) AS unidentified_records
        FROM runs WHERE id IN ({placeholders})
        """,
        child_ids,
    ).fetchone()
    unique_seen = conn.execute(
        f"SELECT COUNT(DISTINCT business_id) AS n FROM run_businesses WHERE run_id IN ({placeholders})",
        child_ids,
    ).fetchone()["n"]
    overlap = conn.execute(
        f"""
        SELECT COUNT(DISTINCT rb.business_id) AS n
        FROM run_businesses rb
        WHERE rb.run_id IN ({placeholders})
          AND rb.business_id IN (
              SELECT business_id FROM run_businesses WHERE run_id = ?
          )
        """,
        [*child_ids, source_run_id],
    ).fetchone()["n"]
    globally_new = conn.execute(
        f"SELECT COUNT(*) AS n FROM businesses WHERE first_run_id IN ({placeholders})",
        child_ids,
    ).fetchone()["n"]
    current_source = conn.execute(
        "SELECT COUNT(*) AS n FROM run_businesses WHERE run_id = ?", (source_run_id,)
    ).fetchone()["n"]
    return {
        "child_runs": len(child_ids),
        "planned_searches": conn.execute(
            "SELECT planned_searches AS n FROM recovery_executions WHERE plan_sha256 = ?",
            (plan_sha256,),
        ).fetchone()["n"],
        "raw_records": sums["raw_records"],
        "accepted_records": sums["accepted_records"],
        "out_of_bounds_records": sums["out_of_bounds_records"],
        "unlocated_records": sums["unlocated_records"],
        "unidentified_records": sums["unidentified_records"],
        "unique_recovery_seen": unique_seen,
        "source_overlap_businesses": overlap,
        "source_increment_businesses": unique_seen - overlap,
        "globally_new_businesses": globally_new,
        "source_membership_at_plan_count": at_plan_count,
        "source_membership_current_count": current_source,
    }


def finalize_recovery_execution(
    conn: sqlite3.Connection, plan_sha256: str, source_run_id: str, at_plan_count: int
) -> dict[str, int]:
    """Verify all children complete, store result_json, mark parent complete.

    Runs inside the caller's transaction so metrics, result storage, and the
    complete status commit atomically.
    """
    incomplete = conn.execute(
        """
        SELECT COUNT(*) AS n FROM recovery_execution_bins rb
        LEFT JOIN runs r ON r.id = rb.run_id
        WHERE rb.plan_sha256 = ?
          AND (rb.run_id IS NULL OR r.id IS NULL OR r.status != 'complete')
        """,
        (plan_sha256,),
    ).fetchone()["n"]
    if incomplete:
        raise RecoverySchemaError("cannot finalize: selected children are not all complete")
    status_row = conn.execute(
        "SELECT status FROM recovery_executions WHERE plan_sha256 = ?", (plan_sha256,)
    ).fetchone()
    if status_row is None or status_row["status"] not in (
        "running", "interrupted", "failed", "complete"
    ):
        raise RecoverySchemaError("cannot finalize: parent status is invalid")
    metrics = compute_recovery_metrics(conn, plan_sha256, source_run_id, at_plan_count)
    result_json = json.dumps(
        metrics, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    conn.execute(
        "UPDATE recovery_executions SET result_json = ?, status = 'complete', "
        "finished_at = ?, error = NULL WHERE plan_sha256 = ?",
        (result_json, utc_now(), plan_sha256),
    )
    _run_fault_hook("after_result_store")
    return metrics
