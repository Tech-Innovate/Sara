from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sara.migrations import MigrationError, apply_migrations, current_schema_version
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


def test_committed_subject_and_subtype_ids_cannot_be_reassigned(tmp_path: Path) -> None:
    conn = _migrated(tmp_path)
    _subject(conn, "be_1", "business_entity")
    _subject(conn, "be_2", "business_entity")
    _subject(conn, "loc_1", "location")
    _subject(conn, "loc_2", "location")
    _subject(conn, "ch_1", "channel")
    _subject(conn, "ch_2", "channel")
    conn.execute(
        "INSERT INTO business_entities(id,created_at,updated_at) VALUES ('be_1','t0','t0')"
    )
    conn.execute(
        "INSERT INTO business_locations(id,business_entity_id,created_at,updated_at) "
        "VALUES ('loc_1','be_1','t0','t0')"
    )
    conn.execute(
        "INSERT INTO channels(id,business_entity_id,location_id,channel_type,identifier,"
        "normalized_identifier,first_observed_at,created_at,updated_at) "
        "VALUES ('ch_1','be_1','loc_1','phone','1','1','t0','t0','t0')"
    )

    with pytest.raises(sqlite3.IntegrityError, match="knowledge subject id is immutable"):
        conn.execute("UPDATE knowledge_subjects SET id='be_x' WHERE id='be_2'")
    with pytest.raises(sqlite3.IntegrityError, match="business entity id is immutable"):
        conn.execute("UPDATE business_entities SET id='be_2' WHERE id='be_1'")
    with pytest.raises(sqlite3.IntegrityError, match="business location id is immutable"):
        conn.execute("UPDATE business_locations SET id='loc_2' WHERE id='loc_1'")
    with pytest.raises(sqlite3.IntegrityError, match="channel id is immutable"):
        conn.execute("UPDATE channels SET id='ch_2' WHERE id='ch_1'")


def test_source_external_identifier_and_relationship_identity_are_durable(tmp_path: Path) -> None:
    conn = _migrated(tmp_path)
    _subject(conn, "be_1", "business_entity")
    _subject(conn, "be_2", "business_entity")
    conn.execute(
        "INSERT INTO business_entities(id,created_at,updated_at) VALUES ('be_1','t0','t0')"
    )
    conn.execute(
        "INSERT INTO business_entities(id,created_at,updated_at) VALUES ('be_2','t0','t0')"
    )
    conn.execute(
        "INSERT INTO sources(id,source_type,name,created_at) VALUES ('src','official_web','Site','t0')"
    )

    with pytest.raises(sqlite3.IntegrityError, match="source identity/type is immutable"):
        conn.execute("UPDATE sources SET source_type='registry' WHERE id='src'")

    conn.execute(
        "INSERT INTO external_identifiers(id,subject_id,source_id,namespace,value,status,"
        "first_observed_at,last_observed_at,created_at) "
        "VALUES ('xid','be_1','src','domain','example.test','active','t0','t0','t0')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="external identifier identity is immutable"):
        conn.execute("UPDATE external_identifiers SET id='xid2' WHERE id='xid'")
    with pytest.raises(sqlite3.IntegrityError, match="external identifier history is durable"):
        conn.execute("DELETE FROM external_identifiers WHERE id='xid'")

    conn.execute(
        "INSERT INTO business_relationships(id,from_entity_id,to_entity_id,relationship_type,status,"
        "first_observed_at,created_at) "
        "VALUES ('rel','be_1','be_2','parent_brand','asserted','t0','t0')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="business relationship identity is immutable"):
        conn.execute(
            "UPDATE business_relationships SET relationship_type='owned_by' WHERE id='rel'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="business relationship history is durable"):
        conn.execute("DELETE FROM business_relationships WHERE id='rel'")


def test_core_partial_identity_index_predicate_must_match_exactly(tmp_path: Path) -> None:
    conn = connect(tmp_path / "bad-index.sqlite")
    conn.execute("DROP INDEX ux_business_place_id")
    conn.execute(
        "CREATE UNIQUE INDEX ux_business_place_id ON businesses(place_id) "
        "WHERE place_id IS NOT NULL AND place_id <> '' AND 0=1"
    )
    conn.commit()

    with pytest.raises(MigrationError, match="partial uniqueness predicate"):
        apply_migrations(conn)
    assert current_schema_version(conn) == 0
