from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from sara.dossier import DossierQueryError, build_business_dossier, main
from sara.maps_backfill import backfill_maps_business_understanding, business_entity_id_for_maps_business
from sara.migrations import apply_migrations
from sara.storage import connect, connect_existing, connect_readonly, ingest_records
from sara.understanding_vocabulary import (
    DOSSIER_DOMAIN_SEED_V1,
    DOSSIER_POLICY_VERSION,
    seed_business_understanding_vocabulary,
)


def _prepared(path: Path):
    conn = connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    return conn


def _add_run(conn, run_id: str, started_at: str) -> None:
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            "test-area",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            2.0,
            1,
            '["restaurant"]',
            "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}',
            f"/evidence/{run_id}.jsonl",
            "running",
            started_at,
        ),
    )
    conn.commit()


def _record(identity: str) -> dict:
    return {
        "place_id": f"place-{identity}",
        "cid": f"cid-{identity}",
        "data_id": f"data-{identity}",
        "title": f"Business {identity}",
        "category": "Restaurant",
        "address": f"Street {identity}",
        "latitude": 21.55,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": f"https://{identity}.example",
        "review_rating": 4.4,
        "review_count": 120,
        "status": "Open",
        "link": f"https://maps.example/{identity}",
    }


def _ingest_complete(conn, run_id: str, identity: str, started_at: str) -> None:
    _add_run(conn, run_id, started_at)
    ingest_records(conn, run_id, [_record(identity)], finalize_run=("complete", 0, None))


def _phase5_fixture(path: Path):
    conn = _prepared(path)
    _ingest_complete(conn, "r1", "seed", "2026-09-25T10:00:00+00:00")
    backfill_maps_business_understanding(conn)
    return conn


def _snapshot(conn) -> dict[str, tuple[tuple, ...]]:
    tables = (
        "runs",
        "businesses",
        "run_businesses",
        "knowledge_subjects",
        "business_entities",
        "business_locations",
        "maps_business_location_links",
        "sources",
        "external_identifiers",
        "acquisition_sessions",
        "evidence_items",
        "observations",
        "facts",
        "fact_observation_support",
        "fact_acquisition_support",
        "dossier_assessments",
        "dossier_domain_assessments",
        "dossier_assessment_seals",
    )
    return {
        table: tuple(tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"))
        for table in tables
    }


def _insert_sealed_assessment(
    conn,
    *,
    assessment_id: str,
    entity_id: str,
    computed_at: str,
    analysis_ready: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO dossier_assessments(id,business_entity_id,policy_version,facts_as_of,"
        "analysis_ready,computed_at,summary_json) VALUES (?,?,?,?,?,?,?)",
        (
            assessment_id,
            entity_id,
            DOSSIER_POLICY_VERSION,
            computed_at,
            analysis_ready,
            computed_at,
            json.dumps({"assessment": assessment_id}, sort_keys=True),
        ),
    )
    for seed in DOSSIER_DOMAIN_SEED_V1:
        conn.execute(
            "INSERT INTO dossier_domain_assessments(assessment_id,domain,state,reason_json,"
            "fact_count,fresh_fact_count) VALUES (?,?, 'partial','{}',0,0)",
            (assessment_id, seed.name),
        )
    conn.execute(
        "INSERT INTO dossier_assessment_seals(assessment_id,sealed_at) VALUES (?,?)",
        (assessment_id, computed_at),
    )


def test_phase5_dossier_is_read_only_and_surfaces_required_layers(tmp_path: Path) -> None:
    db = tmp_path / "dossier.sqlite"
    conn = _phase5_fixture(db)
    before = _snapshot(conn)
    entity_id = business_entity_id_for_maps_business(1)
    conn.close()

    ro = connect_readonly(db)
    assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
    dossier = build_business_dossier(
        ro,
        business_id=1,
        evaluated_at="2026-09-26T00:00:00+00:00",
    )
    assert ro.total_changes == 0
    ro.close()

    check = connect_existing(db)
    assert _snapshot(check) == before
    check.close()

    assert dossier["schema"] == "sara-business-dossier-v1"
    assert dossier["fact_scope"] == "current_only"
    assert dossier["evidence_scope"] == "current_fact_provenance"
    assert dossier["business_entity"]["id"] == entity_id
    assert len(dossier["locations"]) == 1
    assert dossier["locations"][0]["current_for_entity"] is True
    assert len(dossier["facts"]) == 10
    assert len(dossier["evidence"]) == 1
    assert dossier["integrity_issues"] == []
    assert all(fact["observation_support"] for fact in dossier["facts"])
    unresolved = {item["predicate"] for item in dossier["unknowns"]}
    assert "business.offering.service" in unresolved
    assert "capability.online_booking" in unresolved
    assert "location.opening_hours" in unresolved

    preview = {
        item["domain"]: item for item in dossier["dossier_status"]["read_only_preview"]["domains"]
    }
    assert preview["identity"]["state"] == "partial"
    assert preview["offerings"]["state"] == "not_started"
    assert preview["provenance"]["state"] == "partial"
    assert dossier["dossier_status"]["persisted_current_policy"] is None
    assert "effective_analysis_ready" not in dossier["dossier_status"]
    assert dossier["dossier_status"]["read_only_preview"]["analysis_ready"] is False


def test_phase5_cli_never_migrates_or_seeds_an_older_database(tmp_path: Path, capsys) -> None:
    db = tmp_path / "legacy.sqlite"
    conn = connect(db)
    before_objects = tuple(
        tuple(row) for row in conn.execute("SELECT type,name FROM sqlite_master ORDER BY type,name")
    )
    conn.close()

    rc = main(["--db", str(db), "--business-id", "1"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "vocabulary seed" in captured.err.lower()

    check = sqlite3.connect(db)
    after_objects = tuple(
        tuple(row) for row in check.execute("SELECT type,name FROM sqlite_master ORDER BY type,name")
    )
    assert after_objects == before_objects
    assert check.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone() is None
    check.close()


def test_unknown_not_observed_and_confirmed_false_remain_distinct(tmp_path: Path) -> None:
    db = tmp_path / "semantics.sqlite"
    conn = _phase5_fixture(db)
    entity_id = business_entity_id_for_maps_business(1)
    session_id = conn.execute(
        "SELECT id FROM acquisition_sessions WHERE legacy_run_id='r1'"
    ).fetchone()[0]
    evidence_id = conn.execute("SELECT id FROM evidence_items LIMIT 1").fetchone()[0]
    stamp = "2026-09-25T10:00:00+00:00"

    conn.execute(
        "INSERT INTO facts(id,subject_id,predicate,fact_slot,status,valid_from,last_verified_at,"
        "reconciled_at,reconciliation_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "fact_unknown", entity_id, "capability.online_booking", "__single__", "unknown",
            stamp, None, stamp, "test", stamp,
        ),
    )
    conn.execute(
        "INSERT INTO facts(id,subject_id,predicate,fact_slot,status,valid_from,last_verified_at,"
        "reconciled_at,reconciliation_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "fact_not_observed", entity_id, "capability.online_ordering", "__single__",
            "not_observed", stamp, None, stamp, "test", stamp,
        ),
    )
    conn.execute(
        "INSERT INTO fact_acquisition_support(fact_id,acquisition_session_id,support_role) "
        "VALUES (?,?,'supports_absence')",
        ("fact_not_observed", session_id),
    )

    false_json = "false"
    false_hash = hashlib.sha256(false_json.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO observations(id,subject_id,predicate,evidence_id,value_json,normalized_value_json,"
        "value_hash,observation_kind,observed_at,extracted_at,extraction_method,extractor_name,"
        "extractor_version,confidence,created_at) "
        "VALUES (?,?,?,?,?,?,?,'structured_value',?,?,'human_verified','test','1',1.0,?)",
        (
            "obs_false", entity_id, "capability.whatsapp", evidence_id, false_json, false_json,
            false_hash, stamp, stamp, stamp,
        ),
    )
    conn.execute(
        "INSERT INTO facts(id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,"
        "status,valid_from,last_verified_at,reconciled_at,reconciliation_version,created_at) "
        "VALUES (?,?,?,?,?,?,?,'confirmed',?,?,?,?,?)",
        (
            "fact_false", entity_id, "capability.whatsapp", "__single__", false_json, false_json,
            false_hash, stamp, stamp, stamp, "test", stamp,
        ),
    )
    conn.execute(
        "INSERT INTO fact_observation_support(fact_id,observation_id,support_role) "
        "VALUES (?,?,'supports')",
        ("fact_false", "obs_false"),
    )
    conn.commit()
    conn.close()

    ro = connect_readonly(db)
    dossier = build_business_dossier(
        ro, entity_id=entity_id, evaluated_at="2026-09-26T00:00:00+00:00"
    )
    ro.close()

    states = {(item["predicate"], item["state"]) for item in dossier["unknowns"]}
    assert ("capability.online_booking", "unknown") in states
    assert ("capability.online_ordering", "not_observed") in states
    assert all(item["predicate"] != "capability.whatsapp" for item in dossier["unknowns"])
    false_fact = next(fact for fact in dossier["facts"] if fact["id"] == "fact_false")
    assert false_fact["status"] == "confirmed"
    assert false_fact["value"] is False
    not_observed = next(fact for fact in dossier["facts"] if fact["id"] == "fact_not_observed")
    assert not_observed["acquisition_support"][0]["support_role"] == "supports_absence"
    assert dossier["integrity_issues"] == []


def test_provenance_semantic_mismatch_is_visible_not_silently_accepted(tmp_path: Path) -> None:
    db = tmp_path / "mismatch.sqlite"
    conn = _phase5_fixture(db)
    entity_id = business_entity_id_for_maps_business(1)
    name_observation = conn.execute(
        "SELECT id FROM observations WHERE predicate='business.name.trading' LIMIT 1"
    ).fetchone()[0]
    stamp = "2026-09-25T10:00:00+00:00"
    value_json = "false"
    value_hash = hashlib.sha256(value_json.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO facts(id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,"
        "status,valid_from,last_verified_at,reconciled_at,reconciliation_version,created_at) "
        "VALUES (?,?,?,?,?,?,?,'confirmed',?,?,?,?,?)",
        (
            "fact_bad_support", entity_id, "capability.whatsapp", "__single__", value_json,
            value_json, value_hash, stamp, stamp, stamp, "test", stamp,
        ),
    )
    conn.execute(
        "INSERT INTO fact_observation_support(fact_id,observation_id,support_role) "
        "VALUES (?,?,'supports')",
        ("fact_bad_support", name_observation),
    )
    conn.commit()
    conn.close()

    ro = connect_readonly(db)
    dossier = build_business_dossier(
        ro, entity_id=entity_id, evaluated_at="2026-09-26T00:00:00+00:00"
    )
    ro.close()
    assert {
        (item["code"], item["fact_id"]) for item in dossier["integrity_issues"]
    } >= {("support_observation_semantic_mismatch", "fact_bad_support")}


def test_freshness_preview_is_time_explicit_and_deterministic(tmp_path: Path) -> None:
    db = tmp_path / "freshness.sqlite"
    conn = _phase5_fixture(db)
    conn.close()
    ro = connect_readonly(db)
    first = build_business_dossier(
        ro, business_id=1, evaluated_at="2027-09-26T00:00:00+00:00"
    )
    second = build_business_dossier(
        ro, business_id=1, evaluated_at="2027-09-26T00:00:00+00:00"
    )
    ro.close()
    assert first == second
    preview = {
        item["domain"]: item for item in first["dossier_status"]["read_only_preview"]["domains"]
    }
    assert preview["identity"]["state"] == "stale"
    name_fact = next(
        fact for fact in first["facts"] if fact["predicate"] == "business.name.trading"
    )
    assert name_fact["freshness"]["is_stale"] is True


def test_complete_sealed_current_policy_assessment_is_reported_as_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "assessment.sqlite"
    conn = _phase5_fixture(db)
    entity_id = business_entity_id_for_maps_business(1)
    _insert_sealed_assessment(
        conn,
        assessment_id="da_complete",
        entity_id=entity_id,
        computed_at="2026-09-25T12:00:00+00:00",
        analysis_ready=1,
    )
    conn.commit()
    conn.close()

    ro = connect_readonly(db)
    dossier = build_business_dossier(
        ro, entity_id=entity_id, evaluated_at="2026-09-26T00:00:00+00:00"
    )
    ro.close()
    persisted = dossier["dossier_status"]["persisted_current_policy"]
    assert persisted["id"] == "da_complete"
    assert persisted["analysis_ready"] is True
    assert persisted["snapshot_semantics"].startswith("immutable_historical_assessment")
    assert len(persisted["domains"]) == len(DOSSIER_DOMAIN_SEED_V1)
    assert "effective_analysis_ready" not in dossier["dossier_status"]
    assert dossier["dossier_status"]["read_only_preview"]["analysis_ready"] is False


def test_latest_sealed_assessment_uses_absolute_time_not_timestamp_text(tmp_path: Path) -> None:
    db = tmp_path / "assessment-order.sqlite"
    conn = _phase5_fixture(db)
    entity_id = business_entity_id_for_maps_business(1)
    _insert_sealed_assessment(
        conn,
        assessment_id="absolute_newer",
        entity_id=entity_id,
        computed_at="2026-09-25T23:30:00-05:00",
    )
    _insert_sealed_assessment(
        conn,
        assessment_id="text_newer_but_absolute_older",
        entity_id=entity_id,
        computed_at="2026-09-26T03:00:00+00:00",
    )
    conn.commit()
    conn.close()

    ro = connect_readonly(db)
    dossier = build_business_dossier(
        ro, entity_id=entity_id, evaluated_at="2026-09-26T06:00:00+00:00"
    )
    ro.close()
    assert dossier["dossier_status"]["persisted_current_policy"]["id"] == "absolute_newer"


def test_incomplete_sealed_assessment_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "bad-assessment.sqlite"
    conn = _phase5_fixture(db)
    entity_id = business_entity_id_for_maps_business(1)
    stamp = "2026-09-25T12:00:00+00:00"
    conn.execute(
        "INSERT INTO dossier_assessments(id,business_entity_id,policy_version,facts_as_of,"
        "analysis_ready,computed_at,summary_json) VALUES (?,?,?,?,?,?,?)",
        ("da_bad", entity_id, DOSSIER_POLICY_VERSION, stamp, 1, stamp, "{}"),
    )
    conn.execute(
        "INSERT INTO dossier_domain_assessments(assessment_id,domain,state,reason_json,"
        "fact_count,fresh_fact_count) VALUES (?,?, 'sufficient','{}',1,1)",
        ("da_bad", "identity"),
    )
    conn.execute(
        "INSERT INTO dossier_assessment_seals(assessment_id,sealed_at) VALUES (?,?)",
        ("da_bad", stamp),
    )
    conn.commit()
    conn.close()

    ro = connect_readonly(db)
    with pytest.raises(DossierQueryError, match="incomplete domain coverage"):
        build_business_dossier(
            ro, entity_id=entity_id, evaluated_at="2026-09-26T00:00:00+00:00"
        )
    ro.close()


def test_unsynchronized_maps_business_is_rejected_without_side_effects(tmp_path: Path) -> None:
    db = tmp_path / "unsynchronized.sqlite"
    conn = _prepared(db)
    _ingest_complete(conn, "r1", "seed", "2026-09-25T10:00:00+00:00")
    before = _snapshot(conn)
    conn.close()

    ro = connect_readonly(db)
    with pytest.raises(DossierQueryError, match="not synchronized"):
        build_business_dossier(
            ro, business_id=1, evaluated_at="2026-09-26T00:00:00+00:00"
        )
    assert ro.total_changes == 0
    ro.close()

    check = connect_existing(db)
    assert _snapshot(check) == before
    check.close()
