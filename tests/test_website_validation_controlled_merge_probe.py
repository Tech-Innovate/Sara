from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from sara.maps_backfill import backfill_maps_business_understanding
from sara.migrations import apply_migrations
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary
from sara.website_validation import _parser, _validate_arguments
from sara.website_validation_merge_probe import run_controlled_merge_survival_probe


def _add_run(conn, run_id: str) -> None:
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            "test-area",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            1.0,
            1,
            '["restaurant"]',
            "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}',
            f"/evidence/{run_id}.jsonl",
            "running",
            "2026-09-26T00:00:00+00:00",
        ),
    )
    conn.commit()


def _source_database(path: Path, *, with_cid: bool = True) -> None:
    conn = connect(path)
    _add_run(conn, "r1")
    record = {
        "place_id": "place-probe",
        "title": "Controlled Merge Probe",
        "category": "Restaurant",
        "address": "Jeddah",
        "latitude": 21.55,
        "longitude": 39.18,
        "web_site": "https://probe.example/",
        "link": "https://maps.example/probe",
    }
    if with_cid:
        record["cid"] = "cid-probe"
    ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))
    conn.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_controlled_merge_probe_partitions_one_real_business_and_preserves_history(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    probe = tmp_path / "probe.sqlite"
    _source_database(source)
    source_hash_before = _sha256(source)

    result = run_controlled_merge_survival_probe(source, probe, 1)

    assert result.passed is True, result.details
    assert _sha256(source) == source_hash_before
    assert result.details["probe_kind"] == "controlled_complementary_identifier_partition"
    assert result.details["source_business_id"] == 1
    assert result.details["survivor_business_id"] == 1
    assert result.details["synthetic_duplicate_business_id"] != 1
    assert result.details["synthetic_partition_run_id"] == "validation-merge-probe-partition-1"
    assert result.details["synthetic_bridge_run_id"] == "validation-merge-probe-1"
    assert result.details["durable_evidence_count_before"] >= 2
    assert result.details["durable_evidence_count_after"] == result.details["durable_evidence_count_before"]
    assert result.details["missing_evidence_ids"] == []
    assert result.details["observation_history_preserved"] is True
    assert result.details["historical_duplicate_location_visible_as_alias"] is True
    assert result.details["post_merge_anchor_check"]["passed"] is True
    assert result.details["post_merge_foreign_key_violations"] == []
    assert result.details["survivor_dossier_integrity_issues"] == []

    check = sqlite3.connect(probe)
    check.row_factory = sqlite3.Row
    partition = check.execute(
        "SELECT area_name,queries_json,scraper_image,config_json,raw_path,raw_records,"
        "accepted_records,unique_seen,new_businesses "
        "FROM runs WHERE id='validation-merge-probe-partition-1'"
    ).fetchone()
    assert partition is not None
    assert partition["area_name"] == "controlled-validation-merge-probe-partition"
    assert partition["queries_json"] == "[]"
    assert partition["scraper_image"] == "sara-controlled-merge-probe"
    assert partition["raw_path"] is None
    assert tuple(
        partition[field]
        for field in ("raw_records", "accepted_records", "unique_seen", "new_businesses")
    ) == (2, 2, 2, 1)
    assert json.loads(partition["config_json"]) == {
        "validation_kind": "controlled_maps_identity_partition_probe",
        "source_business_id": 1,
        "source_run_id": "r1",
        "synthetic_record": True,
    }

    bridge = check.execute(
        "SELECT area_name,queries_json,scraper_image,config_json,raw_path "
        "FROM runs WHERE id='validation-merge-probe-1'"
    ).fetchone()
    assert bridge is not None
    assert bridge["area_name"] == "controlled-validation-merge-probe"
    assert bridge["queries_json"] == "[]"
    assert bridge["scraper_image"] == "sara-controlled-merge-probe"
    assert bridge["raw_path"] is None
    assert json.loads(bridge["config_json"]) == {
        "validation_kind": "controlled_maps_identity_merge_probe",
        "source_business_id": 1,
        "source_run_id": "r1",
        "synthetic_partition_run_id": "validation-merge-probe-partition-1",
        "synthetic_record": True,
    }

    for synthetic_run_id in (
        "validation-merge-probe-partition-1",
        "validation-merge-probe-1",
    ):
        artifact_refs = {
            row[0]
            for row in check.execute(
                "SELECT e.artifact_ref FROM evidence_items e "
                "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
                "WHERE a.legacy_run_id=?",
                (synthetic_run_id,),
            )
        }
        assert artifact_refs == {None}

    # The real acquisition run remains historical provenance for the original row only;
    # the validation-generated duplicate is never inserted into it.
    real_run_members = {
        int(row[0])
        for row in check.execute(
            "SELECT business_id FROM run_businesses WHERE run_id='r1' ORDER BY business_id"
        )
    }
    assert real_run_members == {1}
    real_run_session_count = int(
        check.execute(
            "SELECT COUNT(*) FROM acquisition_sessions WHERE legacy_run_id='r1'"
        ).fetchone()[0]
    )
    assert real_run_session_count == 0
    check.close()


def test_controlled_merge_probe_requires_two_real_strong_identifiers(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    probe = tmp_path / "probe.sqlite"
    _source_database(source, with_cid=False)

    result = run_controlled_merge_survival_probe(source, probe, 1)

    assert result.passed is False
    assert "needs at least two strong Maps identifiers" in result.details["error"]


def test_controlled_merge_probe_rejects_already_bootstrapped_source_copy(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    probe = tmp_path / "probe.sqlite"
    _source_database(source)
    conn = connect(source)
    apply_migrations(conn)
    seed_business_understanding_vocabulary(conn)
    backfill_maps_business_understanding(conn)
    conn.close()
    source_hash_before = _sha256(source)

    result = run_controlled_merge_survival_probe(source, probe, 1)

    assert result.passed is False
    assert _sha256(source) == source_hash_before
    assert "requires the representative source snapshot before Business Understanding bootstrap" in result.details["error"]


def test_validation_cli_accepts_controlled_probe_without_natural_merge_pair(tmp_path: Path) -> None:
    args = _parser().parse_args(
        [
            "--source-db",
            str(tmp_path / "source.sqlite"),
            "--workspace",
            str(tmp_path / "workspace"),
            "--single-location-business-id",
            "1",
            "--single-location-business-id",
            "2",
            "--multi-branch-group",
            "3,4",
            "--merge-probe-business-id",
            "5",
        ]
    )
    _validate_arguments(args)
    assert args.merge_probe_business_id == 5
    assert args.merge_pair is None
