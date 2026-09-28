from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sara.migrations import apply_migrations
from sara.storage import connect as storage_connect


def _seed_external_identifier(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO knowledge_subjects(id, kind, created_at, updated_at) "
        "VALUES ('be_1', 'business_entity', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO business_entities(id, created_at, updated_at) "
        "VALUES ('be_1', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO sources(id, source_type, name, base_url, created_at, active) "
        "VALUES ('src', 'other', 'Test source', NULL, '2026-01-01T00:00:00+00:00', 1)"
    )
    conn.execute(
        "INSERT INTO external_identifiers("
        "id, subject_id, source_id, namespace, value, status, first_observed_at, "
        "last_observed_at, created_at, status_changed_at"
        ") VALUES ("
        "'xid', 'be_1', 'src', 'test', 'value', 'active', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00', NULL"
        ")"
    )
    conn.commit()


def test_external_identifier_status_transition_timestamp_is_strictly_monotonic(
    tmp_path: Path,
) -> None:
    conn = storage_connect(tmp_path / "status-chronology.sqlite")
    try:
        apply_migrations(conn)
        _seed_external_identifier(conn)

        retired_at = "2026-01-02T00:00:00+00:00"
        reactivated_at = "2026-01-03T00:00:00+00:00"

        # A legacy-compatible row can make its first timestamped transition.
        conn.execute(
            "UPDATE external_identifiers "
            "SET status='retired', status_changed_at=? WHERE id='xid'",
            (retired_at,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, status_changed_at FROM external_identifiers WHERE id='xid'"
        ).fetchone()
        assert tuple(row) == ("retired", retired_at)

        # A status change may not reuse the prior transition timestamp.
        with pytest.raises(
            sqlite3.IntegrityError,
            match="external identifier status changes require a new transition timestamp",
        ):
            conn.execute(
                "UPDATE external_identifiers "
                "SET status='active', status_changed_at=? WHERE id='xid'",
                (retired_at,),
            )
        conn.rollback()

        # A status change may not move the transition timestamp backward.
        with pytest.raises(
            sqlite3.IntegrityError,
            match="external identifier status changes require a new transition timestamp",
        ):
            conn.execute(
                "UPDATE external_identifiers "
                "SET status='active', status_changed_at=? WHERE id='xid'",
                ("2025-12-31T23:59:59+00:00",),
            )
        conn.rollback()

        # Ordinary status-preserving updates remain valid and do not rewrite chronology.
        conn.execute(
            "UPDATE external_identifiers SET last_observed_at=? WHERE id='xid'",
            ("2026-01-02T12:00:00+00:00",),
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, status_changed_at, last_observed_at "
            "FROM external_identifiers WHERE id='xid'"
        ).fetchone()
        assert tuple(row) == (
            "retired",
            retired_at,
            "2026-01-02T12:00:00+00:00",
        )

        # A forward reactivation succeeds and advances the lifecycle timestamp atomically.
        conn.execute(
            "UPDATE external_identifiers "
            "SET status='active', status_changed_at=? WHERE id='xid'",
            (reactivated_at,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, status_changed_at FROM external_identifiers WHERE id='xid'"
        ).fetchone()
        assert tuple(row) == ("active", reactivated_at)
    finally:
        conn.close()
