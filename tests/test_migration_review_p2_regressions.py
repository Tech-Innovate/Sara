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


def _fact_sql() -> str:
    return (
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,"
        "status,valid_from,reconciled_at,reconciliation_version,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    )


def test_affirmative_fact_statuses_require_a_value_representation(tmp_path: Path) -> None:
    conn = _migrated(tmp_path)
    _entity(conn, "be_1")
    conn.execute(
        "INSERT INTO predicate_definitions("
        "name,domain,subject_kind,value_type,cardinality,reconciliation_policy,description"
        ") VALUES ('business.flag','identity','business_entity','boolean','multi','manual_or_conflict','test')"
    )

    for index, status in enumerate(("confirmed", "single_source", "stale"), start=1):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                _fact_sql(),
                (
                    f"fact_missing_{index}",
                    "be_1",
                    "business.flag",
                    f"missing_{index}",
                    None,
                    None,
                    None,
                    status,
                    "t0",
                    "t0",
                    "v1",
                    "t0",
                ),
            )

    conn.execute(
        _fact_sql(),
        (
            "fact_false",
            "be_1",
            "business.flag",
            "confirmed_false",
            "false",
            None,
            None,
            "confirmed",
            "t0",
            "t0",
            "v1",
            "t0",
        ),
    )
    conn.execute(
        _fact_sql(),
        (
            "fact_single",
            "be_1",
            "business.flag",
            "single_normalized",
            None,
            "true",
            None,
            "single_source",
            "t0",
            "t0",
            "v1",
            "t0",
        ),
    )
    conn.execute(
        _fact_sql(),
        (
            "fact_stale",
            "be_1",
            "business.flag",
            "stale_hash",
            None,
            None,
            "a" * 64,
            "stale",
            "t0",
            "t0",
            "v1",
            "t0",
        ),
    )

    for index, status in enumerate(("unknown", "not_observed", "not_applicable", "conflicted"), start=1):
        conn.execute(
            _fact_sql(),
            (
                f"fact_null_{index}",
                "be_1",
                "business.flag",
                f"null_{index}",
                None,
                None,
                None,
                status,
                "t0",
                "t0",
                "v1",
                "t0",
            ),
        )

    assert conn.execute("SELECT value_json FROM facts WHERE id='fact_false'").fetchone()[0] == "false"


def test_channel_endpoint_identity_is_immutable_but_metadata_can_evolve(tmp_path: Path) -> None:
    conn = _migrated(tmp_path)
    _entity(conn, "be_1")
    _subject(conn, "loc_1", "location")
    conn.execute(
        "INSERT INTO business_locations(id,business_entity_id,created_at,updated_at) "
        "VALUES ('loc_1','be_1','t0','t0')"
    )
    _subject(conn, "ch_1", "channel")
    conn.execute(
        "INSERT INTO channels("
        "id,business_entity_id,location_id,channel_type,identifier,normalized_identifier,"
        "url,status,first_observed_at,created_at,updated_at"
        ") VALUES ('ch_1','be_1','loc_1','website','example.com','example.com',"
        "'https://example.com','active','t0','t0','t0')"
    )

    for assignment in (
        "channel_type='phone'",
        "identifier='other.example'",
        "normalized_identifier='other.example'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="channel endpoint identity is immutable"):
            conn.execute(f"UPDATE channels SET {assignment} WHERE id='ch_1'")

    conn.execute(
        "UPDATE channels SET url='https://www.example.com',status='inactive',"
        "last_verified_at='t1',updated_at='t1' WHERE id='ch_1'"
    )
    row = conn.execute(
        "SELECT channel_type,identifier,normalized_identifier,url,status,last_verified_at "
        "FROM channels WHERE id='ch_1'"
    ).fetchone()
    assert tuple(row) == (
        "website",
        "example.com",
        "example.com",
        "https://www.example.com",
        "inactive",
        "t1",
    )
