from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sara.migrations import apply_migrations
from sara.storage import connect
from sara.understanding_vocabulary import seed_business_understanding_vocabulary
from sara.website.model import CrawlConfig
from sara.website_validation import (
    OperationalValidationError,
    _parse_group,
    _parse_pair,
    backup_database,
    compare_legacy_snapshots,
    legacy_snapshot,
    run_acquisition_samples,
    validate_fact_provenance,
    validate_multi_branch_groups,
    validate_one_to_one_maps_anchors,
)


def _migrated(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "sara.sqlite")
    apply_migrations(conn)
    seed_business_understanding_vocabulary(conn)
    return conn


def _insert_business(conn: sqlite3.Connection, business_id: int) -> None:
    conn.execute(
        "INSERT INTO businesses("
        "id,canonical_key,title,first_seen_at,last_seen_at,raw_json"
        ") VALUES (?,?,?,?,?,?)",
        (
            business_id,
            f"place:p{business_id}",
            f"Business {business_id}",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "{}",
        ),
    )


def _insert_anchor(
    conn: sqlite3.Connection,
    business_id: int,
    entity_id: str,
    location_id: str,
) -> None:
    created = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) VALUES (?,?,?,?)",
        (entity_id, "business_entity", created, created),
    )
    conn.execute(
        "INSERT INTO business_entities(id,display_name,created_at,updated_at) VALUES (?,?,?,?)",
        (entity_id, entity_id, created, created),
    )
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) VALUES (?,?,?,?)",
        (location_id, "location", created, created),
    )
    conn.execute(
        "INSERT INTO business_locations(id,business_entity_id,created_at,updated_at) VALUES (?,?,?,?)",
        (location_id, entity_id, created, created),
    )
    conn.execute(
        "INSERT INTO maps_business_location_links(business_id,location_id,linked_at) VALUES (?,?,?)",
        (business_id, location_id, created),
    )


def test_backup_database_is_logically_identical_and_source_is_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    conn = connect(source)
    conn.execute(
        "INSERT INTO businesses(canonical_key,title,first_seen_at,last_seen_at,raw_json) "
        "VALUES ('place:p1','One','2026-01-01','2026-01-01','{}')"
    )
    conn.commit()
    before = legacy_snapshot(conn)
    conn.close()

    copy = tmp_path / "copy.sqlite"
    backup_database(source, copy)

    source_conn = sqlite3.connect(source)
    copy_conn = sqlite3.connect(copy)
    try:
        assert legacy_snapshot(source_conn) == before
        assert legacy_snapshot(copy_conn) == before
    finally:
        source_conn.close()
        copy_conn.close()


def test_legacy_snapshot_comparison_detects_mutation(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    before = legacy_snapshot(conn)
    conn.execute(
        "INSERT INTO businesses(canonical_key,title,first_seen_at,last_seen_at,raw_json) "
        "VALUES ('place:p1','One','2026-01-01','2026-01-01','{}')"
    )
    conn.commit()
    result = compare_legacy_snapshots(before, legacy_snapshot(conn))
    assert result.passed is False
    assert "businesses" in result.details["changed_tables"]
    conn.close()


def test_anchor_and_multi_branch_checks_require_distinct_entities(tmp_path: Path) -> None:
    conn = _migrated(tmp_path)
    for business_id in (1, 2, 3):
        _insert_business(conn, business_id)
        _insert_anchor(conn, business_id, f"be{business_id}", f"loc{business_id}")
    conn.commit()

    assert validate_one_to_one_maps_anchors(conn).passed is True
    assert validate_multi_branch_groups(conn, [(2, 3)]).passed is True

    conn.execute("DELETE FROM maps_business_location_links WHERE business_id=3")
    conn.execute(
        "INSERT INTO maps_business_location_links(business_id,location_id,linked_at) "
        "VALUES (3,'loc2','2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    assert validate_one_to_one_maps_anchors(conn).passed is False
    assert validate_multi_branch_groups(conn, [(2, 3)]).passed is False
    conn.close()


def test_fact_provenance_accepts_value_chain_and_complete_absence_support(tmp_path: Path) -> None:
    conn = _migrated(tmp_path)
    _insert_business(conn, 1)
    _insert_anchor(conn, 1, "be1", "loc1")
    created = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO sources(id,source_type,name,created_at) VALUES ('src','official_website','Site',?)",
        (created,),
    )
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,"
        "status,started_at,finished_at,evidence_count,observation_count"
        ") VALUES ('acq','be1','src','test','1','{}',?,'complete',?,?,1,1)",
        ("0" * 64, created, created),
    )
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_role,status,retrieved_at,content_sha256,metadata_json,created_at"
        ") VALUES ('ev','acq','src','official','usable',?,?,'{}',?)",
        (created, "1" * 64, created),
    )
    conn.execute(
        "INSERT INTO observations("
        "id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,"
        "observation_kind,observed_at,extracted_at,extraction_method,extractor_name,extractor_version,created_at"
        ") VALUES ('obs','be1','business.name.trading','ev','\"One\"','\"One\"',?,"
        "'structured_value',?,?,'deterministic_parser','test','1',?)",
        ("2" * 64, created, created, created),
    )
    conn.execute(
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,status,"
        "valid_from,last_verified_at,reconciled_at,reconciliation_version,created_at"
        ") VALUES ('fact1','be1','business.name.trading','__single__','\"One\"','\"One\"',?,"
        "'single_source',?,?,?,?,?)",
        ("2" * 64, created, created, created, "test-v1", created),
    )
    conn.execute(
        "INSERT INTO fact_observation_support(fact_id,observation_id,support_role) "
        "VALUES ('fact1','obs','supports')"
    )
    conn.execute(
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,status,valid_from,last_verified_at,reconciled_at,"
        "reconciliation_version,created_at"
        ") VALUES ('fact2','be1','capability.online_booking','__single__','not_observed',?,?,?,?,?)",
        (created, created, created, "test-v1", created),
    )
    conn.execute(
        "INSERT INTO fact_acquisition_support(fact_id,acquisition_session_id,support_role) "
        "VALUES ('fact2','acq','supports_absence')"
    )
    conn.commit()

    result = validate_fact_provenance(conn)
    assert result.passed is True

    conn.execute(
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,status,"
        "valid_from,last_verified_at,reconciled_at,reconciliation_version,created_at"
        ") VALUES ('fact3','be1','business.name.trading','other','\"Bad\"','\"Bad\"',?,"
        "'single_source',?,?,?,?,?)",
        ("3" * 64, created, created, created, "test-v1", created),
    )
    conn.commit()
    result = validate_fact_provenance(conn)
    assert result.passed is False
    assert result.details["value_facts_without_full_chain"] == ["fact3"]
    conn.close()


def test_acquisition_sample_runner_repeats_without_network(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    _insert_business(conn, 1)
    conn.commit()
    calls: list[int] = []

    def fake_collector(_conn, *, evidence_root, business_id, config):
        assert evidence_root == tmp_path / "evidence"
        assert isinstance(config, CrawlConfig)
        calls.append(business_id)
        return {"status": "complete", "pages_fetched": 1}

    check, results = run_acquisition_samples(
        conn,
        evidence_root=tmp_path / "evidence",
        business_ids=[1],
        passes=2,
        config=CrawlConfig(),
        collector=fake_collector,
    )
    assert check.passed is True
    assert calls == [1, 1]
    assert [item["pass"] for item in results] == [1, 2]
    conn.close()


def test_sample_argument_parsers_fail_closed() -> None:
    assert _parse_group("1,2,3") == (1, 2, 3)
    assert _parse_pair("4,5") == (4, 5)
    with pytest.raises(Exception):
        _parse_group("1")
    with pytest.raises(Exception):
        _parse_pair("1,2,3")
