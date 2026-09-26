from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .dossier import build_business_dossier
from .maps_backfill import backfill_maps_business_understanding
from .maps_sync import sync_maps_business_understanding
from .migrations import apply_migrations
from .storage import connect_existing, upsert_business
from .understanding_vocabulary import seed_business_understanding_vocabulary
from .website_validation import (
    IDENTITY_FIELDS,
    CheckResult,
    OperationalValidationError,
    _evidence_ids_for_subjects,
    _subject_ids_for_business,
    backup_existing_database,
    validate_one_to_one_maps_anchors,
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _raw_object(row: sqlite3.Row) -> dict[str, Any]:
    try:
        raw = json.loads(str(row["raw_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise OperationalValidationError(
            f"merge-probe business {row['id']} has malformed raw_json"
        ) from exc
    if not isinstance(raw, dict):
        raise OperationalValidationError(
            f"merge-probe business {row['id']} raw_json is not an object"
        )
    return dict(raw)


def _identifier_partition(row: sqlite3.Row) -> tuple[tuple[str, str], list[tuple[str, str]]]:
    identifiers = [
        (field, value)
        for field in IDENTITY_FIELDS
        if (value := _text(row[field])) is not None
    ]
    if len(identifiers) < 2:
        raise OperationalValidationError(
            f"merge-probe business {row['id']} needs at least two strong Maps identifiers; "
            f"found {[field for field, _value in identifiers]!r}"
        )

    canonical_key = str(row["canonical_key"])
    preferred = None
    for item in identifiers:
        field, value = item
        prefix = {"place_id": "place", "cid": "cid", "data_id": "data"}[field]
        if canonical_key == f"{prefix}:{value}":
            preferred = item
            break
    keep = preferred or identifiers[0]
    moved = [item for item in identifiers if item != keep]
    return keep, moved


def _partition_record(
    raw: dict[str, Any],
    identifiers: list[tuple[str, str]],
) -> dict[str, Any]:
    record = dict(raw)
    for field in IDENTITY_FIELDS:
        record.pop(field, None)
    for field, value in identifiers:
        record[field] = value
    return record


def _observation_history_for_evidence(
    conn: sqlite3.Connection, evidence_ids: set[str]
) -> dict[str, tuple[str, str]]:
    if not evidence_ids:
        return {}
    placeholders = ",".join("?" for _ in evidence_ids)
    return {
        str(row["id"]): (str(row["subject_id"]), str(row["evidence_id"]))
        for row in conn.execute(
            "SELECT id,subject_id,evidence_id FROM observations "
            f"WHERE evidence_id IN ({placeholders}) ORDER BY id",
            tuple(sorted(evidence_ids)),
        )
    }


def _bootstrap_probe(conn: sqlite3.Connection) -> None:
    apply_migrations(conn)
    seed_business_understanding_vocabulary(conn)
    business_count = int(conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0])
    link_count = int(
        conn.execute("SELECT COUNT(*) FROM maps_business_location_links").fetchone()[0]
    )
    if link_count:
        raise OperationalValidationError(
            "controlled merge probe requires the representative source snapshot before "
            "Business Understanding bootstrap; use the original Sara database as --source-db"
        )
    if business_count:
        backfill_maps_business_understanding(conn)
    sync_maps_business_understanding(conn)


def _later_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperationalValidationError("merge-probe source run has no valid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperationalValidationError(
            f"merge-probe source run has malformed timestamp {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed.astimezone(timezone.utc) + timedelta(seconds=1)).isoformat()


def _create_bridge_run(
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    business_id: int,
) -> tuple[str, str]:
    source = conn.execute("SELECT * FROM runs WHERE id=?", (source_run_id,)).fetchone()
    if source is None:
        raise OperationalValidationError(f"merge-probe run {source_run_id!r} does not exist")
    bridge_run_id = f"validation-merge-probe-{business_id}"
    if conn.execute("SELECT 1 FROM runs WHERE id=?", (bridge_run_id,)).fetchone() is not None:
        raise OperationalValidationError(
            f"controlled merge-probe run id already exists: {bridge_run_id!r}"
        )
    bridge_started_at = _later_timestamp(source["finished_at"] or source["started_at"])
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,raw_path,"
        "status,started_at,finished_at,exit_code,error,raw_records,accepted_records,"
        "out_of_bounds_records,unlocated_records,unidentified_records,unique_seen,new_businesses"
        ") VALUES (?,?,?,?,?,?,?,?,?,'complete',?,?,0,NULL,1,1,0,0,0,1,0)",
        (
            bridge_run_id,
            source["area_name"],
            source["bbox_json"],
            source["cell_km"],
            source["depth"],
            source["queries_json"],
            source["scraper_image"],
            source["config_json"],
            source["raw_path"],
            bridge_started_at,
            bridge_started_at,
        ),
    )
    return bridge_run_id, bridge_started_at


def run_controlled_merge_survival_probe(
    source_db: Path,
    probe_db: Path,
    business_id: int | None,
) -> CheckResult:
    name = "maps_identity_merge_preserves_understanding_evidence"
    if business_id is None:
        return CheckResult(
            name=name,
            passed=False,
            details={"error": "no controlled merge-probe business configured"},
        )

    backup_existing_database(source_db, probe_db)
    conn = connect_existing(probe_db)
    duplicate_id: int | None = None
    bridge_run_id: str | None = None
    try:
        row = conn.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone()
        if row is None:
            raise OperationalValidationError(
                f"merge-probe business does not exist: {business_id}"
            )
        keep, moved = _identifier_partition(row)
        raw = _raw_object(row)
        keep_record = _partition_record(raw, [keep])
        moved_record = _partition_record(raw, moved)

        source_run_id = str(row["last_run_id"] or "")
        if not source_run_id:
            raise OperationalValidationError(
                f"merge-probe business {business_id} has no last_run_id provenance"
            )
        source_run = conn.execute(
            "SELECT started_at FROM runs WHERE id=?", (source_run_id,)
        ).fetchone()
        if source_run is None:
            raise OperationalValidationError(
                f"merge-probe run {source_run_id!r} does not exist"
            )
        source_started_at = str(source_run["started_at"])

        keep_values = {field: None for field in IDENTITY_FIELDS}
        keep_values[keep[0]] = keep[1]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE businesses SET place_id=?,cid=?,data_id=?,raw_json=? WHERE id=?",
            (
                keep_values["place_id"],
                keep_values["cid"],
                keep_values["data_id"],
                json.dumps(keep_record, ensure_ascii=False, sort_keys=True),
                business_id,
            ),
        )
        duplicate_id, created = upsert_business(
            conn,
            source_run_id,
            moved_record,
            run_started_at=source_started_at,
        )
        if not created or duplicate_id == business_id:
            raise OperationalValidationError(
                "controlled identifier partition did not create a distinct Maps row"
            )
        conn.execute(
            "INSERT OR IGNORE INTO run_businesses(run_id,business_id,first_observed_at) "
            "VALUES (?,?,?)",
            (source_run_id, duplicate_id, source_started_at),
        )
        conn.commit()

        _bootstrap_probe(conn)
        first_subjects = _subject_ids_for_business(conn, business_id)
        duplicate_subjects = _subject_ids_for_business(conn, duplicate_id)
        durable_evidence = _evidence_ids_for_subjects(
            conn, (*first_subjects, *duplicate_subjects)
        )
        if not durable_evidence:
            raise OperationalValidationError(
                "controlled merge probe produced no subject-linked Understanding evidence"
            )
        observation_history = _observation_history_for_evidence(conn, durable_evidence)
        if not observation_history:
            raise OperationalValidationError(
                "controlled merge probe produced no observations for retained evidence"
            )

        bridge = dict(raw)
        for field in IDENTITY_FIELDS:
            bridge.pop(field, None)
        for field, value in [keep, *moved]:
            bridge[field] = value

        conn.execute("BEGIN IMMEDIATE")
        bridge_run_id, bridge_started_at = _create_bridge_run(
            conn,
            source_run_id=source_run_id,
            business_id=business_id,
        )
        survivor_id, created = upsert_business(
            conn,
            bridge_run_id,
            bridge,
            run_started_at=bridge_started_at,
        )
        if created:
            raise OperationalValidationError(
                "controlled bridge unexpectedly created a third Maps business"
            )
        conn.execute(
            "INSERT INTO run_businesses(run_id,business_id,first_observed_at) VALUES (?,?,?)",
            (bridge_run_id, survivor_id, bridge_started_at),
        )
        conn.commit()
        sync_maps_business_understanding(conn)

        placeholders = ",".join("?" for _ in durable_evidence)
        remaining_evidence = {
            str(row[0])
            for row in conn.execute(
                f"SELECT id FROM evidence_items WHERE id IN ({placeholders})",
                tuple(sorted(durable_evidence)),
            )
        }
        observation_history_after = _observation_history_for_evidence(
            conn, durable_evidence
        )
        original_remaining = int(
            conn.execute(
                "SELECT COUNT(*) FROM businesses WHERE id IN (?,?)",
                (business_id, duplicate_id),
            ).fetchone()[0]
        )
        duplicate_location = duplicate_subjects[1]
        duplicate_subject_row = conn.execute(
            "SELECT record_state,merged_into_subject_id FROM knowledge_subjects WHERE id=?",
            (duplicate_location,),
        ).fetchone()
        anchor_check = validate_one_to_one_maps_anchors(conn)
        foreign_key_rows = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        dossier = build_business_dossier(conn, business_id=survivor_id)
        dossier_integrity = list(dossier["integrity_issues"])
        dossier_location_ids = {str(item["id"]) for item in dossier["locations"]}
        duplicate_is_alias = (
            duplicate_subject_row is not None
            and str(duplicate_subject_row["record_state"]) == "merged"
            and duplicate_subject_row["merged_into_subject_id"] is not None
            and duplicate_location in dossier_location_ids
        )

        passed = (
            original_remaining == 1
            and survivor_id == business_id
            and remaining_evidence == durable_evidence
            and observation_history_after == observation_history
            and duplicate_is_alias
            and anchor_check.passed
            and not foreign_key_rows
            and not dossier_integrity
        )
        return CheckResult(
            name=name,
            passed=passed,
            details={
                "probe_kind": "controlled_complementary_identifier_partition",
                "source_business_id": business_id,
                "synthetic_duplicate_business_id": duplicate_id,
                "synthetic_bridge_run_id": bridge_run_id,
                "kept_identifier": {"namespace": keep[0], "value": keep[1]},
                "moved_identifiers": [
                    {"namespace": field, "value": value} for field, value in moved
                ],
                "survivor_business_id": survivor_id,
                "durable_evidence_count_before": len(durable_evidence),
                "durable_evidence_count_after": len(remaining_evidence),
                "missing_evidence_ids": sorted(durable_evidence - remaining_evidence),
                "observation_history_preserved": observation_history_after == observation_history,
                "historical_duplicate_location_id": duplicate_location,
                "historical_duplicate_location_visible_as_alias": duplicate_is_alias,
                "post_merge_anchor_check": asdict(anchor_check),
                "post_merge_foreign_key_violations": foreign_key_rows,
                "survivor_dossier_integrity_issues": dossier_integrity,
            },
        )
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        return CheckResult(
            name=name,
            passed=False,
            details={
                "probe_kind": "controlled_complementary_identifier_partition",
                "source_business_id": business_id,
                "synthetic_duplicate_business_id": duplicate_id,
                "synthetic_bridge_run_id": bridge_run_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    finally:
        conn.close()
