from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sara.migrations import apply_migrations, current_schema_version, MIGRATIONS
from sara.storage import connect as storage_connect
from sara.understanding_vocabulary import (
    DOSSIER_DOMAIN_SEED_V1,
    DOSSIER_POLICY_VERSION,
    FOUNDATION_PREDICATE_SEEDS,
    PREDICATE_SEEDS,
    PREDICATE_SEED_PHASE3,
    PREDICATE_SEED_REVIEW_INTELLIGENCE,
    PREDICATE_SEED_V1,
    VOCABULARY_VERSION,
    VocabularySeedError,
    mandatory_dossier_domains,
    seed_business_understanding_vocabulary,
    verify_business_understanding_vocabulary,
    verify_review_intelligence_vocabulary,
    vocabulary_checksum,
)


EXPECTED_PREDICATE_NAMES_V1 = (
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

EXPECTED_PHASE3_PREDICATE_NAMES = ("location.operating_status",)
EXPECTED_REVIEW_PREDICATE_NAMES = ("reputation.customer_review",)
EXPECTED_FOUNDATION_PREDICATE_NAMES = (
    EXPECTED_PREDICATE_NAMES_V1 + EXPECTED_PHASE3_PREDICATE_NAMES
)
EXPECTED_PREDICATE_NAMES = (
    EXPECTED_FOUNDATION_PREDICATE_NAMES + EXPECTED_REVIEW_PREDICATE_NAMES
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
    assert apply_migrations(conn, migrations=MIGRATIONS[:1]) == (1,)
    assert current_schema_version(conn) == 1
    return conn


def _insert_seeds(conn: sqlite3.Connection, seeds) -> None:
    for seed in seeds:
        conn.execute(
            "INSERT INTO predicate_definitions("
            "name,domain,subject_kind,value_type,cardinality,reconciliation_policy,"
            "freshness_days,description,active) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                seed.name,
                seed.domain,
                seed.subject_kind,
                seed.value_type,
                seed.cardinality,
                seed.reconciliation_policy,
                seed.freshness_days,
                seed.description,
                seed.active,
            ),
        )
    conn.commit()


def test_seed_is_explicit_idempotent_and_does_not_create_business_state(tmp_path: Path) -> None:
    conn = migrated_conn(tmp_path / "seed.sqlite")

    assert seed_business_understanding_vocabulary(conn) == EXPECTED_PREDICATE_NAMES
    assert seed_business_understanding_vocabulary(conn) == ()
    verify_business_understanding_vocabulary(conn)
    verify_review_intelligence_vocabulary(conn)
    assert current_schema_version(conn) == 1

    rows = list(
        conn.execute(
            "SELECT name, domain, subject_kind, value_type, cardinality, "
            "reconciliation_policy, freshness_days, description, active "
            "FROM predicate_definitions ORDER BY rowid"
        )
    )
    assert tuple(row[0] for row in rows) == EXPECTED_PREDICATE_NAMES
    assert len(rows) == len(PREDICATE_SEEDS)

    assert conn.execute("SELECT COUNT(*) FROM knowledge_subjects").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM business_entities").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM business_locations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 0
    assert list(conn.execute("PRAGMA foreign_key_check")) == []
    conn.close()


def test_current_seed_upgrades_an_existing_phase_two_seed(tmp_path: Path) -> None:
    conn = migrated_conn(tmp_path / "phase2-upgrade.sqlite")
    _insert_seeds(conn, PREDICATE_SEED_V1)

    with pytest.raises(VocabularySeedError, match="location.operating_status"):
        verify_business_understanding_vocabulary(conn)
    assert seed_business_understanding_vocabulary(conn) == (
        EXPECTED_PHASE3_PREDICATE_NAMES + EXPECTED_REVIEW_PREDICATE_NAMES
    )
    verify_business_understanding_vocabulary(conn)
    verify_review_intelligence_vocabulary(conn)
    conn.close()


def test_review_predicate_is_additive_to_existing_business_understanding_v2(tmp_path: Path) -> None:
    conn = migrated_conn(tmp_path / "review-upgrade.sqlite")
    _insert_seeds(conn, PREDICATE_SEED_V1 + PREDICATE_SEED_PHASE3)

    verify_business_understanding_vocabulary(conn)
    with pytest.raises(VocabularySeedError, match="reputation.customer_review"):
        verify_review_intelligence_vocabulary(conn)

    assert seed_business_understanding_vocabulary(conn) == EXPECTED_REVIEW_PREDICATE_NAMES
    verify_business_understanding_vocabulary(conn)
    verify_review_intelligence_vocabulary(conn)

    row = conn.execute(
        "SELECT domain,subject_kind,value_type,cardinality,reconciliation_policy,freshness_days "
        "FROM predicate_definitions WHERE name='reputation.customer_review'"
    ).fetchone()
    assert tuple(row) == ("reputation", "location", "json", "multi", "evidence_only", None)
    conn.close()


def test_seed_requires_exact_phase_one_history_and_no_outer_transaction(tmp_path: Path) -> None:
    conn = storage_connect(tmp_path / "unmigrated.sqlite")
    with pytest.raises(VocabularySeedError, match="does not recognize"):
        seed_business_understanding_vocabulary(conn)

    apply_migrations(conn)
    conn.execute("BEGIN")
    with pytest.raises(VocabularySeedError, match="no active transaction"):
        seed_business_understanding_vocabulary(conn)
    conn.rollback()
    conn.close()

    tampered = migrated_conn(tmp_path / "tampered.sqlite")
    tampered.execute(
        "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
        ("0" * 64,),
    )
    tampered.commit()
    with pytest.raises(VocabularySeedError, match="history does not match"):
        seed_business_understanding_vocabulary(tampered)
    tampered.close()

    future = migrated_conn(tmp_path / "future.sqlite")
    future.execute(
        "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
        "VALUES (5, 'future_schema', ?, 't5')",
        ("f" * 64,),
    )
    future.commit()
    with pytest.raises(VocabularySeedError, match="does not recognize"):
        seed_business_understanding_vocabulary(future)
    future.close()


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


def test_verifiers_detect_drift_in_their_required_or_installed_predicates(tmp_path: Path) -> None:
    conn = migrated_conn(tmp_path / "metadata-drift.sqlite")
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "UPDATE predicate_definitions SET freshness_days = 1 "
        "WHERE name = 'business.website.official'"
    )
    conn.commit()

    with pytest.raises(VocabularySeedError, match="business.website.official"):
        seed_business_understanding_vocabulary(conn)
    with pytest.raises(VocabularySeedError, match="business.website.official"):
        verify_business_understanding_vocabulary(conn)
    with pytest.raises(VocabularySeedError, match="business.website.official"):
        verify_review_intelligence_vocabulary(conn)
    conn.close()

    review_drift = migrated_conn(tmp_path / "review-drift.sqlite")
    seed_business_understanding_vocabulary(review_drift)
    review_drift.execute(
        "UPDATE predicate_definitions SET description='drifted' "
        "WHERE name='reputation.customer_review'"
    )
    review_drift.commit()
    with pytest.raises(VocabularySeedError, match="reputation.customer_review"):
        verify_business_understanding_vocabulary(review_drift)
    with pytest.raises(VocabularySeedError, match="reputation.customer_review"):
        verify_review_intelligence_vocabulary(review_drift)
    with pytest.raises(VocabularySeedError, match="reputation.customer_review"):
        seed_business_understanding_vocabulary(review_drift)
    review_drift.close()


def test_domain_policy_matches_the_agreed_business_understanding_surface() -> None:
    assert VOCABULARY_VERSION == "business-understanding-v3"
    assert DOSSIER_POLICY_VERSION == "business-understanding-v1"
    assert tuple(seed.name for seed in PREDICATE_SEED_V1) == EXPECTED_PREDICATE_NAMES_V1
    assert tuple(seed.name for seed in PREDICATE_SEED_PHASE3) == EXPECTED_PHASE3_PREDICATE_NAMES
    assert tuple(seed.name for seed in FOUNDATION_PREDICATE_SEEDS) == EXPECTED_FOUNDATION_PREDICATE_NAMES
    assert (
        tuple(seed.name for seed in PREDICATE_SEED_REVIEW_INTELLIGENCE)
        == EXPECTED_REVIEW_PREDICATE_NAMES
    )
    assert tuple(seed.name for seed in PREDICATE_SEEDS) == EXPECTED_PREDICATE_NAMES
    assert tuple(seed.name for seed in DOSSIER_DOMAIN_SEED_V1) == EXPECTED_DOSSIER_DOMAINS
    assert mandatory_dossier_domains() == EXPECTED_MANDATORY_DOMAINS

    assert len(set(EXPECTED_PREDICATE_NAMES)) == len(EXPECTED_PREDICATE_NAMES)
    assert len(set(EXPECTED_DOSSIER_DOMAINS)) == len(EXPECTED_DOSSIER_DOMAINS)
    assert {seed.domain for seed in PREDICATE_SEEDS} <= set(EXPECTED_DOSSIER_DOMAINS)
    assert set(EXPECTED_MANDATORY_DOMAINS) <= set(EXPECTED_DOSSIER_DOMAINS)
    assert len(vocabulary_checksum()) == 64
