from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sara.migrations import MIGRATIONS, Migration, MigrationError, apply_migrations, current_schema_version
from sara.storage import connect as storage_connect


CORE_SCHEMA = """
CREATE TABLE runs (
    id TEXT PRIMARY KEY, area_name TEXT NOT NULL, bbox_json TEXT NOT NULL,
    cell_km REAL NOT NULL, depth INTEGER NOT NULL, queries_json TEXT NOT NULL,
    scraper_image TEXT NOT NULL, config_json TEXT, raw_path TEXT, status TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, exit_code INTEGER, error TEXT,
    raw_records INTEGER NOT NULL DEFAULT 0, accepted_records INTEGER NOT NULL DEFAULT 0,
    out_of_bounds_records INTEGER NOT NULL DEFAULT 0,
    unlocated_records INTEGER NOT NULL DEFAULT 0,
    unidentified_records INTEGER NOT NULL DEFAULT 0,
    unique_seen INTEGER NOT NULL DEFAULT 0, new_businesses INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE businesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_key TEXT NOT NULL UNIQUE,
    place_id TEXT, cid TEXT, data_id TEXT, title TEXT, category TEXT, address TEXT,
    latitude REAL, longitude REAL, phone TEXT, website TEXT, review_rating REAL,
    review_count INTEGER, status TEXT, first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL, first_run_id TEXT, last_run_id TEXT, raw_json TEXT NOT NULL,
    FOREIGN KEY(first_run_id) REFERENCES runs(id), FOREIGN KEY(last_run_id) REFERENCES runs(id)
);
CREATE UNIQUE INDEX ux_business_place_id ON businesses(place_id)
WHERE place_id IS NOT NULL AND place_id <> '';
CREATE UNIQUE INDEX ux_business_cid ON businesses(cid)
WHERE cid IS NOT NULL AND cid <> '';
CREATE UNIQUE INDEX ux_business_data_id ON businesses(data_id)
WHERE data_id IS NOT NULL AND data_id <> '';
CREATE TABLE run_businesses (
    run_id TEXT NOT NULL, business_id INTEGER NOT NULL, first_observed_at TEXT NOT NULL,
    PRIMARY KEY(run_id, business_id),
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE
);
"""

EXPECTED_TABLES = {
    "schema_migrations", "knowledge_subjects", "business_entities", "business_locations",
    "maps_business_location_links", "sources", "external_identifiers", "channels",
    "acquisition_sessions", "evidence_items", "predicate_definitions", "observations",
    "facts", "fact_observation_support", "fact_acquisition_support", "business_relationships",
    "business_relationship_observation_support", "dossier_assessments", "dossier_domain_assessments",
}


def core_conn(path: Path | None = None, *, row_factory: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:" if path is None else path)
    if row_factory:
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(CORE_SCHEMA)
    return conn


def tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def seed_entity(conn: sqlite3.Connection, entity_id: str = "be_1") -> None:
    conn.execute(
        "INSERT INTO knowledge_subjects(id, kind, created_at, updated_at) VALUES (?, 'business_entity', 't0', 't0')",
        (entity_id,),
    )
    conn.execute(
        "INSERT INTO business_entities(id, created_at, updated_at) VALUES (?, 't0', 't0')",
        (entity_id,),
    )


def seed_evidence(conn: sqlite3.Connection) -> None:
    seed_entity(conn)
    conn.execute("INSERT INTO sources VALUES ('src', 'official_web', 'Site', NULL, 't0', 1)")
    conn.execute(
        "INSERT INTO acquisition_sessions(id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at) VALUES "
        "('acq','be_1','src','test','1','{}',?,'running','t0')",
        ("a" * 64,),
    )
    conn.execute(
        "INSERT INTO evidence_items(id,acquisition_session_id,source_id,source_role,status,retrieved_at,created_at) "
        "VALUES ('ev','acq','src','official','usable','t0','t0')"
    )


def seed_predicate(conn: sqlite3.Connection, *, name: str = "business.name.trading", kind: str = "business_entity") -> None:
    domain = "identity" if kind == "business_entity" else "locations"
    conn.execute(
        "INSERT INTO predicate_definitions(name,domain,subject_kind,value_type,cardinality,reconciliation_policy,description) "
        "VALUES (?,?,?,'text','single','latest_official','test')",
        (name, domain, kind),
    )


def test_apply_is_transactional_idempotent_and_row_factory_independent() -> None:
    conn = core_conn(row_factory=False)
    assert current_schema_version(conn) == 0
    assert apply_migrations(conn) == (1,)
    assert apply_migrations(conn) == ()
    assert current_schema_version(conn) == 1
    assert EXPECTED_TABLES <= tables(conn)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert list(conn.execute("PRAGMA foreign_key_check")) == []
    row = conn.execute("SELECT version,name,checksum FROM schema_migrations").fetchone()
    assert row == (1, MIGRATIONS[0].name, MIGRATIONS[0].checksum)


def test_checksum_unknown_version_and_malformed_registry_fail_closed() -> None:
    conn = core_conn()
    apply_migrations(conn)
    conn.execute("UPDATE schema_migrations SET checksum=? WHERE version=1", ("0" * 64,))
    conn.commit()
    with pytest.raises(MigrationError, match="checksum mismatch"):
        apply_migrations(conn)

    conn = core_conn()
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO schema_migrations VALUES (2,'future',?,'t0')", ("f" * 64,)
    )
    conn.commit()
    with pytest.raises(MigrationError, match="unknown schema migration version 2"):
        apply_migrations(conn)

    conn = core_conn()
    conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL)")
    conn.commit()
    with pytest.raises(MigrationError, match="incompatible layout"):
        apply_migrations(conn)
    assert "knowledge_subjects" not in tables(conn)

    conn = core_conn()
    conn.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,"
        "checksum TEXT NOT NULL,applied_at TEXT NOT NULL)"
    )
    conn.commit()
    with pytest.raises(MigrationError, match="full UNIQUE constraint"):
        apply_migrations(conn)


def test_broken_migration_and_incompatible_core_roll_back() -> None:
    conn = core_conn()
    broken = Migration(1, "broken", (
        "CREATE TABLE transient_table(id INTEGER PRIMARY KEY)", "CREATE TABLE invalid_sql("
    ))
    with pytest.raises(sqlite3.Error):
        apply_migrations(conn, migrations=(broken,))
    assert "transient_table" not in tables(conn)
    assert "schema_migrations" not in tables(conn)

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")
    conn.commit()
    with pytest.raises(MigrationError, match="required Sara core table 'runs'"):
        apply_migrations(conn)
    assert "schema_migrations" not in tables(conn)


def test_phase_one_neither_backfills_nor_changes_core_state() -> None:
    conn = core_conn()
    conn.execute(
        "INSERT INTO runs(id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,status,started_at) "
        "VALUES ('r','a','{}',1,1,'[]','img','complete','t0')"
    )
    bid = conn.execute(
        "INSERT INTO businesses(canonical_key,place_id,title,first_seen_at,last_seen_at,raw_json) "
        "VALUES ('place:p','p','Example','t0','t0','{}')"
    ).lastrowid
    conn.execute("INSERT INTO run_businesses VALUES ('r',?,'t0')", (bid,))
    conn.commit()
    core_names = ("runs", "businesses", "run_businesses", "ux_business_place_id", "ux_business_cid", "ux_business_data_id")
    before_schema = dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE name IN (?,?,?,?,?,?)", core_names
    ))
    before_rows = tuple(tuple(r) for r in conn.execute("SELECT * FROM businesses"))

    apply_migrations(conn)

    after_schema = dict(conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE name IN (?,?,?,?,?,?)", core_names
    ))
    assert after_schema == before_schema
    assert tuple(tuple(r) for r in conn.execute("SELECT * FROM businesses")) == before_rows
    assert conn.execute("SELECT COUNT(*) FROM business_entities").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM business_locations").fetchone()[0] == 0


def test_actual_storage_schema_is_supported_and_partial_identity_predicates_fail_closed(tmp_path: Path) -> None:
    good = storage_connect(tmp_path / "good.sqlite")
    assert apply_migrations(good) == (1,)
    assert good.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert list(good.execute("PRAGMA foreign_key_check")) == []
    good.close()

    bad = storage_connect(tmp_path / "bad.sqlite")
    bad.execute("DROP INDEX ux_business_place_id")
    bad.execute(
        "CREATE UNIQUE INDEX ux_business_place_id ON businesses(place_id) "
        "WHERE place_id = 'only-this-value'"
    )
    bad.commit()
    with pytest.raises(MigrationError, match="partial uniqueness predicate"):
        apply_migrations(bad)
    assert current_schema_version(bad) == 0
    bad.close()


def test_maps_link_cascades_but_location_survives() -> None:
    conn = core_conn(); apply_migrations(conn); seed_entity(conn)
    conn.execute("INSERT INTO knowledge_subjects VALUES ('loc','location','active',NULL,'t0','t0',NULL)")
    conn.execute("INSERT INTO business_locations(id,business_entity_id,created_at,updated_at) VALUES ('loc','be_1','t0','t0')")
    bid = conn.execute(
        "INSERT INTO businesses(canonical_key,first_seen_at,last_seen_at,raw_json) VALUES ('place:p','t0','t0','{}')"
    ).lastrowid
    conn.execute("INSERT INTO maps_business_location_links VALUES (?,'loc','t0')", (bid,)); conn.commit()
    conn.execute("DELETE FROM businesses WHERE id=?", (bid,)); conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM maps_business_location_links").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM business_locations").fetchone()[0] == 1


def test_subject_kind_location_ownership_and_redirect_invariants() -> None:
    conn = core_conn(); apply_migrations(conn); seed_entity(conn, "be_a"); seed_entity(conn, "be_b")
    conn.execute("INSERT INTO knowledge_subjects VALUES ('loc','location','active',NULL,'t0','t0',NULL)")
    conn.execute("INSERT INTO business_locations(id,business_entity_id,created_at,updated_at) VALUES ('loc','be_a','t0','t0')")
    conn.execute("INSERT INTO knowledge_subjects VALUES ('ch','channel','active',NULL,'t0','t0',NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO channels(id,business_entity_id,location_id,channel_type,identifier,normalized_identifier,"
            "first_observed_at,created_at,updated_at) VALUES ('ch','be_b','loc','phone','1','1','t0','t0','t0')"
        )
    conn.execute("INSERT INTO knowledge_subjects VALUES ('wrong','location','active',NULL,'t0','t0',NULL)")
    with pytest.raises(sqlite3.IntegrityError, match="business_entities subject must have kind"):
        conn.execute("INSERT INTO business_entities(id,created_at,updated_at) VALUES ('wrong','t0','t0')")
    with pytest.raises(sqlite3.IntegrityError, match="kind is immutable"):
        conn.execute("UPDATE knowledge_subjects SET kind='location' WHERE id='be_a'")
    with pytest.raises(sqlite3.IntegrityError, match="same kind"):
        conn.execute(
            "UPDATE knowledge_subjects SET record_state='merged',merged_into_subject_id='loc',merged_at='t1' WHERE id='be_a'"
        )

    conn.execute("INSERT INTO knowledge_subjects VALUES ('be_c','business_entity','active',NULL,'t0','t0',NULL)")
    conn.execute("UPDATE knowledge_subjects SET record_state='merged',merged_into_subject_id='be_b',merged_at='t1' WHERE id='be_a'")
    conn.execute("UPDATE knowledge_subjects SET record_state='merged',merged_into_subject_id='be_c',merged_at='t1' WHERE id='be_b'")
    with pytest.raises(sqlite3.IntegrityError, match="create a cycle"):
        conn.execute("UPDATE knowledge_subjects SET record_state='merged',merged_into_subject_id='be_a',merged_at='t1' WHERE id='be_c'")


def test_acquisition_lifecycle_is_consistent_terminal_and_durable() -> None:
    conn = core_conn(); apply_migrations(conn); seed_entity(conn)
    conn.execute("INSERT INTO sources VALUES ('src', 'official_web', 'Site', NULL, 't0', 1)")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO acquisition_sessions(id,target_subject_id,source_id,collector_name,collector_version,"
            "config_json,config_hash,status,started_at) VALUES "
            "('bad','be_1','src','test','1','{}',?,'complete','t0')",
            ("b" * 64,),
        )

    conn.execute(
        "INSERT INTO acquisition_sessions(id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at) VALUES "
        "('acq','be_1','src','test','1','{}',?,'planned','t0')",
        ("a" * 64,),
    )
    conn.execute("UPDATE acquisition_sessions SET status='running' WHERE id='acq'")
    with pytest.raises(sqlite3.IntegrityError, match="invalid acquisition session status transition"):
        conn.execute("UPDATE acquisition_sessions SET status='planned' WHERE id='acq'")
    conn.execute("UPDATE acquisition_sessions SET status='complete',finished_at='t1' WHERE id='acq'")
    with pytest.raises(sqlite3.IntegrityError, match="terminal acquisition session lifecycle is immutable"):
        conn.execute("UPDATE acquisition_sessions SET finished_at='t2' WHERE id='acq'")
    with pytest.raises(sqlite3.IntegrityError, match="terminal acquisition session lifecycle is immutable"):
        conn.execute("UPDATE acquisition_sessions SET status='failed',error='x' WHERE id='acq'")

    conn.execute(
        "INSERT INTO acquisition_sessions(id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at) VALUES "
        "('planned','be_1','src','test','1','{}',?,'planned','t0')",
        ("c" * 64,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="durable history"):
        conn.execute("DELETE FROM acquisition_sessions WHERE id='planned'")


def test_acquisition_evidence_and_identifier_provenance_is_immutable() -> None:
    conn = core_conn(); apply_migrations(conn); seed_evidence(conn)
    conn.execute("INSERT INTO sources VALUES ('src2','other','Other',NULL,'t0',1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO evidence_items(id,acquisition_session_id,source_id,source_role,status,retrieved_at,created_at) "
            "VALUES ('bad','acq','src2','third_party','usable','t0','t0')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="evidence items are immutable"):
        conn.execute("UPDATE evidence_items SET status='incomplete' WHERE id='ev'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM evidence_items WHERE id='ev'")
    with pytest.raises(sqlite3.IntegrityError, match="acquisition session identity/configuration is immutable"):
        conn.execute("UPDATE acquisition_sessions SET config_json='x' WHERE id='acq'")
    conn.execute("UPDATE acquisition_sessions SET status='partial',finished_at='t1' WHERE id='acq'")
    with pytest.raises(sqlite3.IntegrityError, match="terminal acquisition session lifecycle is immutable"):
        conn.execute("UPDATE acquisition_sessions SET status='running',finished_at=NULL WHERE id='acq'")

    conn.execute("INSERT INTO external_identifiers VALUES ('xid','be_1','src','key','v','active','t0','t0','t0')")
    with pytest.raises(sqlite3.IntegrityError, match="external identifier identity is immutable"):
        conn.execute("UPDATE external_identifiers SET value='other' WHERE id='xid'")


def test_observation_fact_and_predicate_semantics_are_guarded() -> None:
    conn = core_conn(); apply_migrations(conn); seed_evidence(conn); seed_predicate(conn)
    conn.execute(
        "INSERT INTO observations(id,subject_id,predicate,evidence_id,value_json,observation_kind,extracted_at,"
        "extraction_method,extractor_name,extractor_version,created_at) VALUES "
        "('obs','be_1','business.name.trading','ev','\"A\"','source_assertion','t0','deterministic_parser','x','1','t0')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="observations are immutable"):
        conn.execute("UPDATE observations SET value_json='\"B\"' WHERE id='obs'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM observations WHERE id='obs'")
    with pytest.raises(sqlite3.IntegrityError, match="predicate semantics are immutable"):
        conn.execute("UPDATE predicate_definitions SET subject_kind='location' WHERE name='business.name.trading'")
    conn.execute("UPDATE predicate_definitions SET freshness_days=90 WHERE name='business.name.trading'")

    seed_predicate(conn, name="location.address", kind="location")
    with pytest.raises(sqlite3.IntegrityError, match="observation subject kind does not match"):
        conn.execute(
            "INSERT INTO observations(id,subject_id,predicate,evidence_id,observation_kind,extracted_at,"
            "extraction_method,extractor_name,extractor_version,created_at) VALUES "
            "('bad','be_1','location.address','ev','source_assertion','t0','model','x','1','t0')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fact subject kind does not match"):
        conn.execute(
            "INSERT INTO facts(id,subject_id,predicate,fact_slot,status,valid_from,reconciled_at,reconciliation_version,created_at) "
            "VALUES ('badf','be_1','location.address','__single__','unknown','t0','t0','v1','t0')"
        )


def test_fact_versions_and_support_are_append_only_history() -> None:
    conn = core_conn(); apply_migrations(conn); seed_evidence(conn); seed_predicate(conn)
    conn.execute(
        "INSERT INTO observations(id,subject_id,predicate,evidence_id,value_json,observation_kind,extracted_at,"
        "extraction_method,extractor_name,extractor_version,created_at) VALUES "
        "('obs','be_1','business.name.trading','ev','\"A\"','source_assertion','t0','deterministic_parser','x','1','t0')"
    )
    conn.execute(
        "INSERT INTO facts(id,subject_id,predicate,fact_slot,value_json,status,valid_from,reconciled_at,reconciliation_version,created_at) "
        "VALUES ('f1','be_1','business.name.trading','__single__','\"A\"','single_source','t0','t0','v1','t0')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM facts WHERE id='f1'")
    conn.execute("INSERT INTO fact_observation_support VALUES ('f1','obs','supports')")
    conn.execute("INSERT INTO fact_acquisition_support VALUES ('f1','acq','context')")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE fact_observation_support SET support_role='contradicts' WHERE fact_id='f1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM fact_observation_support WHERE fact_id='f1'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE fact_acquisition_support SET support_role='searched' WHERE fact_id='f1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM fact_acquisition_support WHERE fact_id='f1'")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO facts(id,subject_id,predicate,fact_slot,value_json,status,valid_from,reconciled_at,reconciliation_version,created_at) "
            "VALUES ('f2','be_1','business.name.trading','__single__','\"B\"','single_source','t1','t1','v1','t1')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fact versions are immutable"):
        conn.execute("UPDATE facts SET status='confirmed' WHERE id='f1'")
    conn.execute("UPDATE facts SET valid_to='t1' WHERE id='f1'")
    with pytest.raises(sqlite3.IntegrityError, match="fact versions are immutable"):
        conn.execute("UPDATE facts SET valid_to='t2' WHERE id='f1'")
    conn.execute(
        "INSERT INTO facts(id,subject_id,predicate,fact_slot,value_json,status,valid_from,reconciled_at,reconciliation_version,created_at) "
        "VALUES ('f2','be_1','business.name.trading','__single__','\"B\"','single_source','t1','t1','v1','t1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO facts(id,subject_id,predicate,fact_slot,value_json,status,valid_from,reconciled_at,reconciliation_version,created_at) "
            "VALUES ('bad','be_1','business.name.trading','other','false','not_observed','t2','t2','v1','t2')"
        )


def test_dossier_snapshots_are_immutable() -> None:
    conn = core_conn(); apply_migrations(conn); seed_entity(conn)
    conn.execute("INSERT INTO dossier_assessments VALUES ('da','be_1','v1','t0',0,'t0','{}')")
    conn.execute("INSERT INTO dossier_domain_assessments VALUES ('da','identity','partial','{}',1,1)")
    with pytest.raises(sqlite3.IntegrityError, match="immutable snapshots"):
        conn.execute("UPDATE dossier_assessments SET analysis_ready=1 WHERE id='da'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable snapshots"):
        conn.execute("UPDATE dossier_domain_assessments SET state='strong' WHERE assessment_id='da'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable snapshots"):
        conn.execute("DELETE FROM dossier_domain_assessments WHERE assessment_id='da'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable snapshots"):
        conn.execute("DELETE FROM dossier_assessments WHERE id='da'")


def test_registry_ordering_gaps_and_applied_history_are_rejected() -> None:
    conn = core_conn()
    with pytest.raises(MigrationError, match="strictly ordered"):
        apply_migrations(conn, migrations=(Migration(2,"two",()), Migration(1,"one",())))
    with pytest.raises(MigrationError, match="contiguous"):
        apply_migrations(conn, migrations=(Migration(1,"one",()), Migration(3,"three",())))

    migrations = (
        Migration(1,"one",("CREATE TABLE m1(id INTEGER PRIMARY KEY)",)),
        Migration(2,"two",("CREATE TABLE m2(id INTEGER PRIMARY KEY)",)),
        Migration(3,"three",("CREATE TABLE m3(id INTEGER PRIMARY KEY)",)),
    )
    conn.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,"
        "checksum TEXT NOT NULL,applied_at TEXT NOT NULL,CHECK(version>0),CHECK(length(checksum)=64))"
    )
    for m in (migrations[0], migrations[2]):
        conn.execute("INSERT INTO schema_migrations VALUES (?,?,?,'t0')", (m.version,m.name,m.checksum))
    conn.commit()
    with pytest.raises(MigrationError, match="not a contiguous prefix"):
        apply_migrations(conn, migrations=migrations)


def test_read_only_and_bad_core_constraints_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    conn = core_conn(path); conn.close()
    ro = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.Error):
        apply_migrations(ro)
    ro.close()
    assert current_schema_version(sqlite3.connect(path)) == 0

    conn = sqlite3.connect(":memory:"); conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(CORE_SCHEMA.replace(
        "FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,\n    FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE",
        "UNIQUE(run_id,business_id)",
    ))
    with pytest.raises(MigrationError, match="missing required foreign key"):
        apply_migrations(conn)
