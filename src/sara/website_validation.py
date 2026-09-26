from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .maps_backfill import backfill_maps_business_understanding
from .maps_sync import sync_maps_business_understanding
from .migrations import apply_migrations
from .storage import connect_readonly
from .understanding_vocabulary import (
    DOSSIER_DOMAIN_SEED_V1,
    seed_business_understanding_vocabulary,
)


REPORT_SCHEMA = "sara-website-operational-validation-v1"
LEGACY_TABLES = (
    "runs",
    "businesses",
    "run_businesses",
    "recovery_executions",
    "recovery_execution_bins",
)
IDENTITY_FIELDS = ("place_id", "cid", "data_id")


class OperationalValidationError(RuntimeError):
    """Operational validation cannot be completed safely."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    details: dict[str, Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _row_dicts(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    cursor = conn.execute(f'SELECT * FROM "{table}"')
    columns = [str(item[0]) for item in cursor.description or ()]
    rows = [
        {column: row[index] for index, column in enumerate(columns)}
        for row in cursor.fetchall()
    ]
    rows.sort(key=_canonical_json)
    return rows


def logical_table_fingerprint(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    if not _table_exists(conn, table):
        return {"present": False, "row_count": 0, "sha256": None}
    rows = _row_dicts(conn, table)
    payload = _canonical_json(rows).encode("utf-8")
    return {
        "present": True,
        "row_count": len(rows),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def legacy_snapshot(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {table: logical_table_fingerprint(conn, table) for table in LEGACY_TABLES}


def compare_legacy_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> CheckResult:
    changed = {
        table: {"before": before.get(table), "after": after.get(table)}
        for table in LEGACY_TABLES
        if before.get(table) != after.get(table)
    }
    return CheckResult(
        name="legacy_collection_recovery_tables_unchanged",
        passed=not changed,
        details={"changed_tables": changed},
    )


def backup_database(source_db: str | Path, destination_db: str | Path) -> None:
    source_path = Path(source_db)
    destination_path = Path(destination_db)
    if not source_path.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_path}")
    if destination_path.exists():
        raise OperationalValidationError(
            f"validation database already exists: {destination_path}"
        )
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = connect_readonly(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


def backup_existing_database(source_db: str | Path, destination_db: str | Path) -> None:
    source_path = Path(source_db)
    destination_path = Path(destination_db)
    if destination_path.exists():
        destination_path.unlink()
    source = connect_readonly(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


def bootstrap_understanding(conn: sqlite3.Connection) -> dict[str, Any]:
    migrations = apply_migrations(conn)
    seeded = seed_business_understanding_vocabulary(conn)
    business_count = int(conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0])
    link_count = int(
        conn.execute("SELECT COUNT(*) FROM maps_business_location_links").fetchone()[0]
    )
    backfill = None
    if business_count and link_count == 0:
        backfill = asdict(backfill_maps_business_understanding(conn))
    sync = asdict(sync_maps_business_understanding(conn))
    return {
        "migrations_applied": list(migrations),
        "predicates_seeded": list(seeded),
        "backfill": backfill,
        "sync": sync,
    }


def validate_one_to_one_maps_anchors(conn: sqlite3.Connection) -> CheckResult:
    business_count = int(conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0])
    rows = list(
        conn.execute(
            "SELECT m.business_id,m.location_id,bl.business_entity_id "
            "FROM maps_business_location_links m "
            "JOIN business_locations bl ON bl.id=m.location_id "
            "ORDER BY m.business_id"
        )
    )
    distinct_businesses = len({int(row[0]) for row in rows})
    distinct_locations = len({str(row[1]) for row in rows})
    missing = [
        int(row[0])
        for row in conn.execute(
            "SELECT b.id FROM businesses b "
            "LEFT JOIN maps_business_location_links m ON m.business_id=b.id "
            "WHERE m.business_id IS NULL ORDER BY b.id"
        )
    ]
    passed = (
        len(rows) == business_count
        and distinct_businesses == business_count
        and distinct_locations == business_count
        and not missing
    )
    return CheckResult(
        name="maps_one_to_one_understanding_anchors",
        passed=passed,
        details={
            "business_count": business_count,
            "link_count": len(rows),
            "distinct_businesses": distinct_businesses,
            "distinct_locations": distinct_locations,
            "missing_business_ids": missing,
        },
    )


def validate_multi_branch_groups(
    conn: sqlite3.Connection,
    groups: Sequence[Sequence[int]],
) -> CheckResult:
    failures: list[dict[str, Any]] = []
    inspected: list[dict[str, Any]] = []
    for group in groups:
        ids = tuple(int(value) for value in group)
        placeholders = ",".join("?" for _ in ids)
        rows = list(
            conn.execute(
                "SELECT m.business_id,bl.business_entity_id "
                "FROM maps_business_location_links m "
                "JOIN business_locations bl ON bl.id=m.location_id "
                f"WHERE m.business_id IN ({placeholders}) ORDER BY m.business_id",
                ids,
            )
        )
        mapping = {int(row[0]): str(row[1]) for row in rows}
        distinct_entities = set(mapping.values())
        item = {
            "business_ids": list(ids),
            "entity_ids": {str(key): value for key, value in mapping.items()},
        }
        inspected.append(item)
        if len(mapping) != len(ids) or len(distinct_entities) != len(ids):
            failures.append(item)
    return CheckResult(
        name="multi_branch_samples_remain_deliberately_separated",
        passed=bool(groups) and not failures,
        details={"groups_inspected": inspected, "failures": failures},
    )


def validate_fact_provenance(conn: sqlite3.Connection) -> CheckResult:
    value_statuses = ("confirmed", "single_source", "conflicted", "stale")
    placeholders = ",".join("?" for _ in value_statuses)
    unsupported_value_facts = [
        str(row[0])
        for row in conn.execute(
            "SELECT f.id FROM facts f "
            f"WHERE f.status IN ({placeholders}) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM fact_observation_support fos "
            "  JOIN observations o ON o.id=fos.observation_id "
            "  JOIN evidence_items e ON e.id=o.evidence_id "
            "  JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
            "  JOIN sources s ON s.id=a.source_id "
            "  WHERE fos.fact_id=f.id AND fos.support_role='supports'"
            ") ORDER BY f.id",
            value_statuses,
        )
    ]
    unsupported_not_observed = [
        str(row[0])
        for row in conn.execute(
            "SELECT f.id FROM facts f "
            "WHERE f.status='not_observed' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM fact_acquisition_support fas "
            "  JOIN acquisition_sessions a ON a.id=fas.acquisition_session_id "
            "  JOIN sources s ON s.id=a.source_id "
            "  WHERE fas.fact_id=f.id "
            "    AND fas.support_role='supports_absence' "
            "    AND a.status='complete'"
            ") ORDER BY f.id"
        )
    ]
    return CheckResult(
        name="fact_provenance_is_complete",
        passed=not unsupported_value_facts and not unsupported_not_observed,
        details={
            "value_facts_without_full_chain": unsupported_value_facts,
            "not_observed_without_complete_absence_support": unsupported_not_observed,
        },
    )


def validate_dossier_readiness_integrity(conn: sqlite3.Connection) -> CheckResult:
    mandatory = {
        seed.name for seed in DOSSIER_DOMAIN_SEED_V1 if seed.mandatory_for_initial_analysis
    }
    bad: list[dict[str, Any]] = []
    rows = list(
        conn.execute(
            "SELECT da.id,da.business_entity_id "
            "FROM finalized_dossier_assessments da "
            "WHERE da.analysis_ready=1 ORDER BY da.id"
        )
    )
    for assessment_id, entity_id in rows:
        domain_rows = list(
            conn.execute(
                "SELECT domain,state FROM dossier_domain_assessments "
                "WHERE assessment_id=? ORDER BY domain",
                (assessment_id,),
            )
        )
        actual = {str(row[0]): str(row[1]) for row in domain_rows}
        below = sorted(
            domain
            for domain in mandatory
            if actual.get(domain) not in {"sufficient", "strong", "not_applicable"}
        )
        if below:
            bad.append(
                {
                    "assessment_id": str(assessment_id),
                    "business_entity_id": str(entity_id),
                    "mandatory_domains_below_sufficient": below,
                }
            )
    return CheckResult(
        name="persisted_analysis_ready_assessments_are_internally_consistent",
        passed=not bad,
        details={
            "analysis_ready_assessments_checked": len(rows),
            "invalid_assessments": bad,
        },
    )


def _business_exists(conn: sqlite3.Connection, business_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM businesses WHERE id=?", (business_id,)).fetchone()
        is not None
    )


def _subject_ids_for_business(
    conn: sqlite3.Connection, business_id: int
) -> tuple[str, str]:
    row = conn.execute(
        "SELECT m.location_id,bl.business_entity_id "
        "FROM maps_business_location_links m "
        "JOIN business_locations bl ON bl.id=m.location_id "
        "WHERE m.business_id=?",
        (business_id,),
    ).fetchone()
    if row is None:
        raise OperationalValidationError(
            f"business {business_id} has no Understanding location link"
        )
    return str(row[1]), str(row[0])


def _evidence_ids_for_subjects(
    conn: sqlite3.Connection, subject_ids: Iterable[str]
) -> set[str]:
    ids = tuple(subject_ids)
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT evidence_id FROM observations "
            f"WHERE subject_id IN ({placeholders}) ORDER BY evidence_id",
            ids,
        )
    }


def _bridge_record(first: sqlite3.Row, second: sqlite3.Row) -> dict[str, Any]:
    bridge: dict[str, Any] = {}
    matched_rows: set[int] = set()
    for field in IDENTITY_FIELDS:
        left = first[field]
        right = second[field]
        values = {str(value) for value in (left, right) if value not in (None, "")}
        if len(values) > 1:
            raise OperationalValidationError(
                f"merge pair has conflicting {field} values: {sorted(values)!r}"
            )
        if values:
            value = next(iter(values))
            bridge[field] = value
            if left == value:
                matched_rows.add(int(first["id"]))
            if right == value:
                matched_rows.add(int(second["id"]))
    if len(matched_rows) != 2:
        raise OperationalValidationError(
            "merge pair cannot be bridged by complementary strong identifiers"
        )
    for field in (
        "title",
        "category",
        "address",
        "latitude",
        "longitude",
        "phone",
        "website",
        "review_rating",
        "review_count",
        "status",
    ):
        bridge[field] = first[field] if first[field] not in (None, "") else second[field]
    return bridge


def _parse_group(value: str) -> tuple[int, ...]:
    try:
        items = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("business IDs must be integers") from exc
    if len(items) < 2 or any(item <= 0 for item in items) or len(set(items)) != len(items):
        raise argparse.ArgumentTypeError(
            "multi-branch group must contain at least two distinct positive business IDs"
        )
    return items


def _parse_pair(value: str) -> tuple[int, int]:
    items = _parse_group(value)
    if len(items) != 2:
        raise argparse.ArgumentTypeError("merge pair must contain exactly two business IDs")
    return items[0], items[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sara-website-validate",
        description=(
            "Run the official-website production-readiness gate on disposable copies "
            "of a representative Sara database."
        ),
    )
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument(
        "--single-location-business-id",
        action="append",
        type=int,
        default=[],
        help="known single-location Maps business ID; specify at least two",
    )
    parser.add_argument(
        "--multi-branch-group",
        action="append",
        type=_parse_group,
        default=[],
        help="comma-separated Maps business IDs belonging to one known multi-branch brand",
    )
    parser.add_argument(
        "--merge-pair",
        type=_parse_pair,
        help="comma-separated complementary-identifier Maps business IDs for the merge-survival probe",
    )
    parser.add_argument(
        "--passes",
        type=int,
        default=2,
        help="bounded acquisition passes per representative business (default: 2)",
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.passes < 2 or args.passes > 3:
        raise OperationalValidationError("passes must be between 2 and 3")
    singles = [int(value) for value in args.single_location_business_id]
    if len(singles) < 2 or len(set(singles)) != len(singles):
        raise OperationalValidationError(
            "provide at least two distinct --single-location-business-id values"
        )
    if not args.multi_branch_group:
        raise OperationalValidationError("provide at least one --multi-branch-group")
    if args.merge_pair is None:
        raise OperationalValidationError("provide --merge-pair for the evidence-survival probe")
    workspace = Path(args.workspace)
    if workspace.exists() and any(workspace.iterdir()):
        raise OperationalValidationError(
            f"workspace must be absent or empty: {workspace}"
        )
