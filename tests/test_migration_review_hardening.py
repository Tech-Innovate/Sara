from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sara.migrations import apply_migrations
from sara.storage import connect


def _migrated(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "sara.sqlite")
    assert apply_migrations(conn) == (1,)
    return conn


def _subject(conn: sqlite3.Connection, subject_id: str, kind: str) -> None:
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) VALUES (?,?,?,?)",
        (subject_id, kind, "t0", "t0"),
    )


def _entity(conn: sqlite3.Connection, entity_id: str) -> None:
    _subject(conn, entity_id, "business_entity")
    conn.execute(
        "INSERT INTO business_entities(id,created_at,updated_at) VALUES (?,?,?)",
        (entity_id, "t0", "t0"),
    )


def _seed_observation(conn: sqlite3.Connection) -> None:
    _entity(conn, "be_1")
    conn.execute(
        "INSERT INTO sources(id,source_type,name,created_at) VALUES ('src','official_web','Site','t0')"
    )
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,"
        "status,started_at"
        ") VALUES ('acq','be_1','src','test','1','{}',?,'running','t0')",
        ("a" * 64,),
    )
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_role,status,retrieved_at,created_at"
        ") VALUES ('ev','acq','src','official','usable','t0','t0')"
    )
    conn.execute(
        "INSERT INTO predicate_definitions("
        "name,domain,subject_kind,value_type,cardinality,reconciliation_policy,description"
        ") VALUES ('business.name.trading','identity','business_entity','text','single',"
        "'latest_official','test')"
    )
    conn.execute(
        "INSERT INTO observations("
        "id,subject_id,predicate,evidence_id,value_json,observation_kind,extracted_at,"
        "extraction_method,extractor_name,extractor_version,created_at"
        ") VALUES ('obs','be_1','business.name.trading','ev','\"Alpha\"','source_assertion',"
        "'t0','direct_structured','test','1','t0')"
    )


def test_replace_cannot_rewrite_append_only_history_with_recursive_triggers_off(
    tmp_path: Path,
) -> None:
    conn = _migrated(tmp_path)
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    _seed_observation(conn)

    with pytest.raises(sqlite3.IntegrityError, match="observation identity already exists"):
        conn.execute(
            "INSERT OR REPLACE INTO observations("
            "id,subject_id,predicate,evidence_id,value_json,observation_kind,extracted_at,"
            "extraction_method,extractor_name,extractor_version,created_at"
            ") VALUES ('obs','be_1','business.name.trading','ev','\"Beta\"','source_assertion',"
            "'t1','direct_structured','test','1','t1')"
        )
    assert conn.execute("SELECT value_json FROM observations WHERE id='obs'").fetchone()[0] == '"Alpha"'

    conn.execute(
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,status,valid_from,reconciled_at,"
        "reconciliation_version,created_at"
        ") VALUES ('fact_1','be_1','business.name.trading','__single__','\"Alpha\"',"
        "'single_source','t0','t0','v1','t0')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="fact identity or current slot already exists"):
        conn.execute(
            "INSERT OR REPLACE INTO facts("
            "id,subject_id,predicate,fact_slot,value_json,status,valid_from,reconciled_at,"
            "reconciliation_version,created_at"
            ") VALUES ('fact_2','be_1','business.name.trading','__single__','\"Beta\"',"
            "'single_source','t1','t1','v1','t1')"
        )


def test_location_and_channel_ownership_cannot_be_reassigned_or_reinserted(
    tmp_path: Path,
) -> None:
    conn = _migrated(tmp_path)
    _entity(conn, "be_a")
    _entity(conn, "be_b")
    _subject(conn, "loc", "location")
    _subject(conn, "loc2", "location")
    conn.execute(
        "INSERT INTO business_locations(id,business_entity_id,created_at,updated_at) "
        "VALUES ('loc','be_a','t0','t0')"
    )
    conn.execute(
        "INSERT INTO business_locations(id,business_entity_id,created_at,updated_at) "
        "VALUES ('loc2','be_a','t0','t0')"
    )

    with pytest.raises(sqlite3.IntegrityError, match="business location ownership is immutable"):
        conn.execute("UPDATE business_locations SET business_entity_id='be_b' WHERE id='loc'")
    with pytest.raises(sqlite3.IntegrityError, match="business location subjects are durable"):
        conn.execute("DELETE FROM business_locations WHERE id='loc2'")
    with pytest.raises(sqlite3.IntegrityError, match="business location identity already exists"):
        conn.execute(
            "INSERT OR REPLACE INTO business_locations(id,business_entity_id,created_at,updated_at) "
            "VALUES ('loc2','be_b','t0','t1')"
        )

    _subject(conn, "ch", "channel")
    conn.execute(
        "INSERT INTO channels("
        "id,business_entity_id,location_id,channel_type,identifier,normalized_identifier,"
        "first_observed_at,created_at,updated_at"
        ") VALUES ('ch','be_a','loc','phone','1','1','t0','t0','t0')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="channel ownership/location scope is immutable"):
        conn.execute(
            "UPDATE channels SET business_entity_id='be_b',location_id=NULL WHERE id='ch'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="channel subjects are durable"):
        conn.execute("DELETE FROM channels WHERE id='ch'")


def test_dossier_snapshot_is_not_final_until_sealed_and_cannot_grow_after_seal(
    tmp_path: Path,
) -> None:
    conn = _migrated(tmp_path)
    _entity(conn, "be_1")
    conn.execute(
        "INSERT INTO dossier_assessments("
        "id,business_entity_id,policy_version,facts_as_of,analysis_ready,computed_at,summary_json"
        ") VALUES ('da','be_1','v1','t0',0,'t0','{}')"
    )
    assert conn.execute("SELECT COUNT(*) FROM finalized_dossier_assessments").fetchone()[0] == 0

    with pytest.raises(sqlite3.IntegrityError, match="cannot be sealed without domain assessments"):
        conn.execute("INSERT INTO dossier_assessment_seals VALUES ('da','t0')")

    conn.execute(
        "INSERT INTO dossier_domain_assessments "
        "VALUES ('da','identity','partial','{}',1,1)"
    )
    conn.execute("INSERT INTO dossier_assessment_seals VALUES ('da','t1')")
    row = conn.execute(
        "SELECT id,sealed_at FROM finalized_dossier_assessments WHERE id='da'"
    ).fetchone()
    assert tuple(row) == ("da", "t1")

    with pytest.raises(sqlite3.IntegrityError, match="sealed dossier assessment cannot accept"):
        conn.execute(
            "INSERT INTO dossier_domain_assessments "
            "VALUES ('da','offerings','not_started','{}',0,0)"
        )
    with pytest.raises(sqlite3.IntegrityError, match="dossier assessment seals are immutable"):
        conn.execute("UPDATE dossier_assessment_seals SET sealed_at='t2' WHERE assessment_id='da'")
    with pytest.raises(sqlite3.IntegrityError, match="dossier assessment seals are durable"):
        conn.execute("DELETE FROM dossier_assessment_seals WHERE assessment_id='da'")
