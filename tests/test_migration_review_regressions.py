from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sara.migrations import MigrationError, apply_migrations, current_schema_version
from sara.storage import SCHEMA, connect


def _migrated(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "sara.sqlite")
    assert apply_migrations(conn) == (1,)
    return conn


def _subject(conn: sqlite3.Connection, subject_id: str, kind: str) -> None:
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) VALUES (?,?,?,?)",
        (subject_id, kind, "t0", "t0"),
    )


def _single_predicate(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT INTO predicate_definitions("
        "name,domain,subject_kind,value_type,cardinality,reconciliation_policy,description"
        ") VALUES (?,?,?,?,?,?,?)",
        (name, "identity", "business_entity", "text", "single", "latest_official", "test"),
    )


def _fact_sql() -> str:
    return (
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,status,valid_from,reconciled_at,"
        "reconciliation_version,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)"
    )


def test_single_valued_facts_require_canonical_slot_and_one_current_value(
    tmp_path: Path,
) -> None:
    conn = _migrated(tmp_path)
    _subject(conn, "be_1", "business_entity")
    conn.execute(
        "INSERT INTO business_entities(id,created_at,updated_at) VALUES ('be_1','t0','t0')"
    )
    _single_predicate(conn, "business.name.trading")

    with pytest.raises(
        sqlite3.IntegrityError, match="single-valued facts must use __single__ slot"
    ):
        conn.execute(
            _fact_sql(),
            (
                "fact_bad",
                "be_1",
                "business.name.trading",
                "alternate",
                '"Alpha"',
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
            "fact_1",
            "be_1",
            "business.name.trading",
            "__single__",
            '"Alpha"',
            "single_source",
            "t0",
            "t0",
            "v1",
            "t0",
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        conn.execute(
            _fact_sql(),
            (
                "fact_2",
                "be_1",
                "business.name.trading",
                "__single__",
                '"Beta"',
                "single_source",
                "t1",
                "t1",
                "v1",
                "t1",
            ),
        )


def test_core_declared_column_types_are_validated_before_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "wrong-type.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        SCHEMA.replace(
            "id INTEGER PRIMARY KEY AUTOINCREMENT,",
            "id TEXT PRIMARY KEY,",
            1,
        )
    )
    conn.commit()

    with pytest.raises(MigrationError, match="incompatible declared column types"):
        apply_migrations(conn)

    assert current_schema_version(conn) == 0
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='maps_business_location_links'"
        ).fetchone()
        is None
    )
