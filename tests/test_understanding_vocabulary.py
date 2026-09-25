from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sara.migrations import apply_migrations, current_schema_version
from sara.storage import connect as storage_connect
from sara.understanding_vocabulary import (
    DOSSIER_DOMAIN_SEED_V1,
    DOSSIER_POLICY_VERSION,
    PREDICATE_SEED_V1,
    VOCABULARY_VERSION,
    VocabularySeedError,
    mandatory_dossier_domains,
    seed_business_understanding_vocabulary,
    vocabulary_checksum,
)


EXPECTED_PREDICATE_NAMES = (
    "business.name.trading",
    "business.category.primary",
    "business.website.official",
    "business.model.transaction_type",
    "business.customer_segment.stated",
    "business.offering.service",
    "location.address",
    "location.latitude",
    "location.longitude",
    "location.phone",
    "location.opening_hours",
    "capability.online_booking",
    "capability.online_ordering",
    "capability.whatsapp",
    "reputation.rating",
    "reputation.review_count",
)

EXPECTED_DOSSIER_DOMAINS = (
    "identity",
    "classification",
    "locations",
    "offerings",
    "customer_market",
    "business_model",
    "scale",
    "communication",
    "digital_presence",
    "digital_capabilities",
    "customer_journey",
    "reputation",
    "marketing",
    "technology",
    "people",
    "operations",
    "change",
    "competitive_context",
    "provenance",
    "unknowns",
)

EXPECTED_MANDATORY_DOMAINS = (
    "identity",
    "classification",
    "locations",
    "offerings",
    "business_model",
    "communication",
    "digital_presence",
    "digital_capabilities",
    "customer_journey",
    "reputation",
    "competitive_context",
    "provenance",
    "unknowns",
)


def migrated_conn(path: Path) -> sqlite3.Connection:
    conn = storage_connect(path)
    assert apply_migrations(conn) == (1,)
    assert current_schema_version(conn) == 1
    return conn


def test_seed_is_explicit_idempotent_and_does_not_create_business_state(tmp_path: Path) -> None:
    conn = migrated_conn(tmp_path / "seed.sqlite")

    assert seed_business_understanding_vocabulary(conn) == EXPECTED_PREDICATE_NAMES
    assert seed_business_understanding_vocabulary(conn) == ()
    assert current_schema_version(conn) == 1

    rows = list(
        conn.execute(
            "SELECT name, domain, subject_kind, value_type, cardinality, "
            "reconciliation_policy, freshness_days, description, active "
            "FROM predicate_definitions ORDER BY rowid"
        )
    )
    assert tuple(row[0] for row in rows) == EXPECTED_PREDICATE_NAMES
    assert len(rows) == len(PREDICATE_SEED_V1)

    assert conn.execute("SELECT COUNT(*) FROM knowledge_subjects").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM business_entities").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM business_locations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 0
    assert list(conn.execute("PRAGMA foreign_key_check")) == []
    conn.close()


def test_seed_requires_phase_one_and_no_outer_transaction(tmp_path: Path) -> None:
    conn = storage_connect(tmp_path / "unmigrated.sqlite")
    with pytest.raises(VocabularySeedError, match="migration v1"):
        seed_business_understanding_vocabulary(conn)

    apply_migrations(conn)
    conn.execute("BEGIN")
    with pytest.raises(VocabularySeedError, match="no active transaction"):
        seed_business_understanding_vocabulary(conn)
    conn.rollback()
    conn.close()


def test_seed_fails_closed_and_rolls_back_partial_inserts_on_semantic_drift(tmp_path: Path) -> None:
    conn = migrated_conn(tmp_path / "drift.sqlite")

    conflicting = PREDICATE_SEED_V1[2]
    conn.execute(
        "INSERT INTO predicate_definitions("
        "name, domain, subject_kind, value_type, cardinality, reconciliation_policy, "
        "freshness_days, description, active"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            conflicting.name,
            conflicting.domain,
            conflicting.subject_kind,
            conflicting.value_type,
            conflicting.cardinality,
            "intentionally_different",
            conflicting.freshness_days,
            conflicting.description,
            conflicting.active,
        ),
    )
    conn.commit()

    with pytest.raises(VocabularySeedError, match="predicate seed drift"):
        seed_business_understanding_vocabulary(conn)

    rows = list(conn.execute("SELECT name FROM predicate_definitions ORDER BY name"))
    assert [row[0] for row in rows] == [conflicting.name]
    conn.close()


def test_seed_detects_post_install_drift_in_mutable_metadata(tmp_path: Path) -> None:
    conn = migrated_conn(tmp_path / "metadata-drift.sqlite")
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "UPDATE predicate_definitions SET freshness_days = 1 "
        "WHERE name = 'business.website.official'"
    )
    conn.commit()

    with pytest.raises(VocabularySeedError, match="business.website.official"):
        seed_business_understanding_vocabulary(conn)
    conn.close()


def test_domain_policy_matches_the_agreed_business_understanding_surface() -> None:
    assert VOCABULARY_VERSION == "business-understanding-v1"
    assert DOSSIER_POLICY_VERSION == "business-understanding-v1"
    assert tuple(seed.name for seed in PREDICATE_SEED_V1) == EXPECTED_PREDICATE_NAMES
    assert tuple(seed.name for seed in DOSSIER_DOMAIN_SEED_V1) == EXPECTED_DOSSIER_DOMAINS
    assert mandatory_dossier_domains() == EXPECTED_MANDATORY_DOMAINS

    assert len(set(EXPECTED_PREDICATE_NAMES)) == len(EXPECTED_PREDICATE_NAMES)
    assert len(set(EXPECTED_DOSSIER_DOMAINS)) == len(EXPECTED_DOSSIER_DOMAINS)
    assert {seed.domain for seed in PREDICATE_SEED_V1} <= set(EXPECTED_DOSSIER_DOMAINS)
    assert set(EXPECTED_MANDATORY_DOMAINS) <= set(EXPECTED_DOSSIER_DOMAINS)
    assert len(vocabulary_checksum()) == 64
