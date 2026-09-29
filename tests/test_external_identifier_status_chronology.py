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


def test_mixed_offset_earlier_instant_is_rejected(tmp_path: Path) -> None:
    """A +03:00 timestamp whose instant is earlier must not pass as advancing."""
    conn = storage_connect(tmp_path / "mixed-offset-earlier.sqlite")
    try:
        apply_migrations(conn)
        _seed_external_identifier(conn)
        conn.execute(
            "UPDATE external_identifiers "
            "SET status='retired', status_changed_at='2026-01-02T10:00:00+00:00' WHERE id='xid'"
        )
        conn.commit()
        # 12:00+03:00 is 09:00Z, one hour EARLIER than the stored 10:00Z,
        # although the string sorts after it.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE external_identifiers "
                "SET status='superseded', status_changed_at='2026-01-02T12:00:00+03:00' "
                "WHERE id='xid'"
            )
        conn.rollback()
        row = conn.execute(
            "SELECT status, status_changed_at FROM external_identifiers WHERE id='xid'"
        ).fetchone()
        assert tuple(row) == ("retired", "2026-01-02T10:00:00+00:00")
    finally:
        conn.close()


def test_mixed_offset_valid_later_instant_is_accepted(tmp_path: Path) -> None:
    """A later instant expressed with a non-UTC offset must be accepted."""
    conn = storage_connect(tmp_path / "mixed-offset-later.sqlite")
    try:
        apply_migrations(conn)
        _seed_external_identifier(conn)
        conn.execute(
            "UPDATE external_identifiers "
            "SET status='retired', status_changed_at='2026-01-02T12:00:00+03:00' WHERE id='xid'"
        )
        conn.commit()
        # 12:00+03:00 is 09:00Z; 10:30+00:00 is 10:30Z, a genuinely later
        # instant although the string sorts before the stored value.
        conn.execute(
            "UPDATE external_identifiers "
            "SET status='superseded', status_changed_at='2026-01-02T10:30:00+00:00' "
            "WHERE id='xid'"
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, status_changed_at FROM external_identifiers WHERE id='xid'"
        ).fetchone()
        assert tuple(row) == ("superseded", "2026-01-02T10:30:00+00:00")
    finally:
        conn.close()


def test_fractional_seconds_with_offset_parse_correctly(tmp_path: Path) -> None:
    """datetime.isoformat() output (fractional seconds + offset) must order by instant."""
    conn = storage_connect(tmp_path / "fractional.sqlite")
    try:
        apply_migrations(conn)
        _seed_external_identifier(conn)
        conn.execute(
            "UPDATE external_identifiers "
            "SET status='retired', status_changed_at='2026-01-02T10:00:00.100000+00:00' "
            "WHERE id='xid'"
        )
        conn.commit()

        # 12:30:00.000000+03:00 is 09:30Z: an earlier instant whose string
        # sorts after the stored value and whose old fixed-position parser
        # would have read offset digits from the fraction.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE external_identifiers "
                "SET status='superseded', "
                "status_changed_at='2026-01-02T12:30:00.000000+03:00' WHERE id='xid'"
            )
        conn.rollback()

        # A 100ms advance at the same offset is genuinely later.
        conn.execute(
            "UPDATE external_identifiers "
            "SET status='superseded', "
            "status_changed_at='2026-01-02T10:00:00.200000+00:00' WHERE id='xid'"
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, status_changed_at FROM external_identifiers WHERE id='xid'"
        ).fetchone()
        assert row[1] == "2026-01-02T10:00:00.200000+00:00"
    finally:
        conn.close()


def test_naive_timestamp_is_rejected_not_treated_as_utc(tmp_path: Path) -> None:
    """A transition timestamp without a UTC offset must fail closed."""
    conn = storage_connect(tmp_path / "naive.sqlite")
    try:
        apply_migrations(conn)
        _seed_external_identifier(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE external_identifiers "
                "SET status='retired', status_changed_at='2026-01-02T11:00:00' "
                "WHERE id='xid'"
            )
        conn.rollback()
        row = conn.execute(
            "SELECT status, status_changed_at FROM external_identifiers WHERE id='xid'"
        ).fetchone()
        assert tuple(row) == ("active", None)
    finally:
        conn.close()


def test_malformed_offset_is_rejected(tmp_path: Path) -> None:
    """A malformed ±HH:MM tail must fail closed, not parse as digits."""
    conn = storage_connect(tmp_path / "malformed.sqlite")
    try:
        apply_migrations(conn)
        _seed_external_identifier(conn)
        for malformed in (
            "2026-01-02T11:00:00+0300",  # missing colon
            "2026-01-02T11:00:00+3:00",  # one-digit hour
            "2026-01-02T11:00:00+99:00",  # out-of-range hour
            "2026-01-02T11:00:00+00:99",  # out-of-range minute
            "2026-01-02T11:00:00.5+00:0Z",  # garbage tail
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE external_identifiers "
                    "SET status='retired', status_changed_at=? WHERE id='xid'",
                    (malformed,),
                )
            conn.rollback()
    finally:
        conn.close()


def test_invalid_stored_timestamp_fails_closed_on_transition(tmp_path: Path) -> None:
    """A malformed stored timestamp blocks any later transition attempt."""
    conn = storage_connect(tmp_path / "stored-malformed.sqlite")
    try:
        apply_migrations(conn)
        _seed_external_identifier(conn)
        # INSERT is not trigger-guarded, so legacy corruption can persist.
        conn.execute(
            "INSERT INTO external_identifiers("
            "id, subject_id, source_id, namespace, value, status, first_observed_at, "
            "last_observed_at, created_at, status_changed_at"
            ") VALUES ("
            "'xid2', 'be_1', 'src', 'test', 'value2', 'retired', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00', '2026-01-02T11:00:00+0300'"
            ")"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE external_identifiers "
                "SET status='superseded', "
                "status_changed_at='2026-01-03T00:00:00+00:00' WHERE id='xid2'"
            )
        conn.rollback()
    finally:
        conn.close()


def test_one_microsecond_advance_is_accepted(tmp_path: Path) -> None:
    """A genuine 1us forward transition must not collapse to equality."""
    conn = storage_connect(tmp_path / "one-microsecond.sqlite")
    try:
        apply_migrations(conn)
        _seed_external_identifier(conn)
        conn.execute(
            "UPDATE external_identifiers "
            "SET status='retired', status_changed_at='2026-01-02T10:00:00.000001+00:00' "
            "WHERE id='xid'"
        )
        conn.commit()
        conn.execute(
            "UPDATE external_identifiers "
            "SET status='superseded', status_changed_at='2026-01-02T10:00:00.000002+00:00' "
            "WHERE id='xid'"
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, status_changed_at FROM external_identifiers WHERE id='xid'"
        ).fetchone()
        assert row[1] == "2026-01-02T10:00:00.000002+00:00"
        # The same microsecond again is not an advance.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE external_identifiers "
                "SET status='active', status_changed_at='2026-01-02T10:00:00.000002+00:00' "
                "WHERE id='xid'"
            )
        conn.rollback()
    finally:
        conn.close()


def test_mixed_offset_microsecond_ordering(tmp_path: Path) -> None:
    """Microsecond ordering holds across differing UTC offsets."""
    conn = storage_connect(tmp_path / "mixed-microsecond.sqlite")
    try:
        apply_migrations(conn)
        _seed_external_identifier(conn)
        conn.execute(
            "UPDATE external_identifiers "
            "SET status='retired', status_changed_at='2026-01-02T10:00:00.000005+00:00' "
            "WHERE id='xid'"
        )
        conn.commit()
        # 12:00:00.000003+03:00 is 09:00:00.000003Z: earlier by two microseconds.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE external_identifiers "
                "SET status='superseded', "
                "status_changed_at='2026-01-02T12:00:00.000003+03:00' WHERE id='xid'"
            )
        conn.rollback()
        # 07:00:00.000007-03:00 is 10:00:00.000007Z: later by two microseconds.
        conn.execute(
            "UPDATE external_identifiers "
            "SET status='superseded', "
            "status_changed_at='2026-01-02T07:00:00.000007-03:00' WHERE id='xid'"
        )
        conn.commit()
        row = conn.execute(
            "SELECT status, status_changed_at FROM external_identifiers WHERE id='xid'"
        ).fetchone()
        assert row[1] == "2026-01-02T07:00:00.000007-03:00"
    finally:
        conn.close()


def test_hour_twenty_four_is_rejected(tmp_path: Path) -> None:
    """Hour 24 parses in SQLite date functions but not in Sara's boundary."""
    conn = storage_connect(tmp_path / "hour-24.sqlite")
    try:
        apply_migrations(conn)
        _seed_external_identifier(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE external_identifiers "
                "SET status='retired', status_changed_at='2026-01-02T24:00:00+00:00' "
                "WHERE id='xid'"
            )
        conn.rollback()
        row = conn.execute(
            "SELECT status, status_changed_at FROM external_identifiers WHERE id='xid'"
        ).fetchone()
        assert tuple(row) == ("active", None)
    finally:
        conn.close()
