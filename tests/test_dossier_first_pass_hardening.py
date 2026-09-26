from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from sara.dossier.core import _freshness
from sara.dossier.provenance import attach_provenance
from sara.dossier.status import persisted_assessment
from sara.understanding_vocabulary import DOSSIER_DOMAIN_SEED_V1, DOSSIER_POLICY_VERSION


def test_not_observed_freshness_ages_without_becoming_confirmed_absence() -> None:
    fact = {
        "id": "fact_not_observed",
        "status": "not_observed",
        "freshness_days": 30,
        "last_verified_at": None,
        "valid_from": "2026-09-01T00:00:00+00:00",
    }
    freshness = _freshness(fact, datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert freshness["evaluated"] is True
    assert freshness["is_stale"] is True
    assert fact["status"] == "not_observed"


def test_unknown_and_not_applicable_are_not_given_synthetic_freshness() -> None:
    for status in ("unknown", "not_applicable"):
        fact = {
            "id": f"fact_{status}",
            "status": status,
            "freshness_days": 30,
            "last_verified_at": None,
            "valid_from": "2026-09-01T00:00:00+00:00",
        }
        freshness = _freshness(fact, datetime(2027, 1, 1, tzinfo=timezone.utc))
        assert freshness["evaluated"] is False
        assert freshness["is_stale"] is False


def _provenance_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sources (
            id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            name TEXT NOT NULL,
            base_url TEXT,
            active INTEGER NOT NULL
        );
        CREATE TABLE acquisition_sessions (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            collector_name TEXT NOT NULL,
            collector_version TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            legacy_run_id TEXT
        );
        CREATE TABLE evidence_items (
            id TEXT PRIMARY KEY,
            acquisition_session_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_locator TEXT,
            source_role TEXT NOT NULL,
            status TEXT NOT NULL,
            retrieved_at TEXT NOT NULL,
            published_at TEXT,
            language TEXT,
            media_type TEXT,
            content_sha256 TEXT,
            artifact_ref TEXT
        );
        CREATE TABLE observations (
            id TEXT PRIMARY KEY,
            subject_id TEXT NOT NULL,
            predicate TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            value_json TEXT,
            normalized_value_json TEXT,
            value_hash TEXT,
            observation_kind TEXT NOT NULL,
            observed_at TEXT,
            extracted_at TEXT NOT NULL,
            extraction_method TEXT NOT NULL,
            extractor_name TEXT NOT NULL,
            extractor_version TEXT NOT NULL,
            confidence REAL
        );
        CREATE TABLE fact_observation_support (
            fact_id TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            support_role TEXT NOT NULL
        );
        CREATE TABLE fact_acquisition_support (
            fact_id TEXT NOT NULL,
            acquisition_session_id TEXT NOT NULL,
            support_role TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO sources VALUES ('source','official_web','Official',NULL,1)")
    conn.execute(
        "INSERT INTO acquisition_sessions VALUES "
        "('session','source','collector','1','complete','2026-09-01T00:00:00+00:00',"
        "'2026-09-01T00:01:00+00:00',NULL)"
    )
    conn.execute(
        "INSERT INTO evidence_items VALUES "
        "('e1','session','source','https://example.test/a','official','usable',"
        "'2026-09-01T00:00:00+00:00',NULL,NULL,'text/html',NULL,NULL)"
    )
    conn.execute(
        "INSERT INTO evidence_items VALUES "
        "('e2','session','source','https://example.test/b','official','usable',"
        "'2026-09-01T00:00:00+00:00',NULL,NULL,'text/html',NULL,NULL)"
    )
    for observation_id, evidence_id in (("o1", "e1"), ("o2", "e2")):
        value_json = json.dumps(f"https://{observation_id}.example")
        conn.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?,NULL,'structured_value',"
            "'2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00',"
            "'human_verified','test','1',1.0)",
            (
                observation_id,
                "entity",
                "business.website.official",
                evidence_id,
                value_json,
                value_json,
            ),
        )
    return conn


def _conflicted_fact() -> dict:
    return {
        "id": "fact_conflict",
        "subject_id": "entity",
        "predicate": "business.website.official",
        "value": None,
        "normalized_value": None,
        "value_hash": None,
        "status": "conflicted",
        "observation_support": [],
        "acquisition_support": [],
    }


def _selected_fact(value: str) -> dict:
    return {
        "id": "fact_selected",
        "subject_id": "entity",
        "predicate": "business.website.official",
        "value": value,
        "normalized_value": value,
        "value_hash": None,
        "status": "confirmed",
        "observation_support": [],
        "acquisition_support": [],
    }


def test_conflicted_fact_accepts_distinct_linked_observations_without_chosen_support_role() -> None:
    conn = _provenance_db()
    conn.execute("INSERT INTO fact_observation_support VALUES ('fact_conflict','o1','contradicts')")
    conn.execute("INSERT INTO fact_observation_support VALUES ('fact_conflict','o2','contradicts')")
    evidence, issues = attach_provenance(conn, [_conflicted_fact()])
    codes = {item["code"] for item in issues}
    assert "value_fact_without_supporting_observation" not in codes
    assert "conflicted_fact_without_multiple_observations" not in codes
    assert "conflicted_fact_without_distinct_observation_values" not in codes
    assert {item["value"] for item in evidence[0]["observations"]} == {"https://o1.example"}


def test_conflicted_fact_with_one_observation_is_flagged() -> None:
    conn = _provenance_db()
    conn.execute("INSERT INTO fact_observation_support VALUES ('fact_conflict','o1','contradicts')")
    _evidence, issues = attach_provenance(conn, [_conflicted_fact()])
    assert {item["code"] for item in issues} >= {
        "conflicted_fact_without_multiple_observations",
        "conflicted_fact_without_distinct_observation_values",
    }


def test_selected_fact_support_value_mismatch_is_visible_and_observation_value_is_exposed() -> None:
    conn = _provenance_db()
    conn.execute("INSERT INTO fact_observation_support VALUES ('fact_selected','o1','supports')")
    evidence, issues = attach_provenance(conn, [_selected_fact("https://chosen.example")])
    assert {item["code"] for item in issues} >= {"support_observation_value_mismatch"}
    assert evidence[0]["observations"][0]["value"] == "https://o1.example"
    assert evidence[0]["observations"][0]["normalized_value"] == "https://o1.example"


def test_support_from_nonusable_evidence_is_visible() -> None:
    conn = _provenance_db()
    conn.execute("UPDATE evidence_items SET status='malformed' WHERE id='e1'")
    conn.execute("INSERT INTO fact_observation_support VALUES ('fact_selected','o1','supports')")
    _evidence, issues = attach_provenance(conn, [_selected_fact("https://o1.example")])
    assert {item["code"] for item in issues} >= {"fact_support_uses_nonusable_evidence"}


def _assessment_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE finalized_dossier_assessments (
            id TEXT PRIMARY KEY,
            business_entity_id TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            facts_as_of TEXT NOT NULL,
            analysis_ready INTEGER NOT NULL,
            computed_at TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE dossier_domain_assessments (
            assessment_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            state TEXT NOT NULL,
            reason_json TEXT NOT NULL,
            fact_count INTEGER NOT NULL,
            fresh_fact_count INTEGER NOT NULL
        );
        """
    )
    return conn


def _insert_assessment(
    conn: sqlite3.Connection,
    *,
    assessment_id: str,
    analysis_ready: int,
    mandatory_state: str,
    computed_at: str = "2026-09-25T12:00:00+00:00",
    facts_as_of: str | None = None,
    sealed_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO finalized_dossier_assessments VALUES (?,?,?,?,?,?,?,?)",
        (
            assessment_id,
            "entity",
            DOSSIER_POLICY_VERSION,
            facts_as_of or computed_at,
            analysis_ready,
            computed_at,
            json.dumps({"id": assessment_id}, sort_keys=True),
            sealed_at or computed_at,
        ),
    )
    for seed in DOSSIER_DOMAIN_SEED_V1:
        state = mandatory_state if seed.mandatory_for_initial_analysis else "partial"
        conn.execute(
            "INSERT INTO dossier_domain_assessments VALUES (?,?,?,?,?,?)",
            (assessment_id, seed.name, state, "{}", 1, 1),
        )


def test_analysis_ready_snapshot_with_sufficient_mandatory_domains_has_no_integrity_issue() -> None:
    conn = _assessment_db()
    _insert_assessment(
        conn,
        assessment_id="ready",
        analysis_ready=1,
        mandatory_state="sufficient",
    )
    assessment = persisted_assessment(conn, "entity")
    assert assessment is not None
    assert assessment["analysis_ready"] is True
    assert assessment["integrity_issues"] == []


def test_analysis_ready_snapshot_with_partial_mandatory_domain_is_flagged() -> None:
    conn = _assessment_db()
    _insert_assessment(
        conn,
        assessment_id="bad_ready",
        analysis_ready=1,
        mandatory_state="partial",
    )
    assessment = persisted_assessment(conn, "entity")
    assert assessment is not None
    assert {item["code"] for item in assessment["integrity_issues"]} >= {
        "analysis_ready_with_mandatory_domain_below_sufficient"
    }


def test_assessment_chronology_inconsistencies_are_flagged() -> None:
    conn = _assessment_db()
    _insert_assessment(
        conn,
        assessment_id="bad_time",
        analysis_ready=0,
        mandatory_state="partial",
        computed_at="2026-09-25T12:00:00+00:00",
        facts_as_of="2026-09-25T13:00:00+00:00",
        sealed_at="2026-09-25T11:00:00+00:00",
    )
    assessment = persisted_assessment(conn, "entity")
    assert assessment is not None
    assert {item["code"] for item in assessment["integrity_issues"]} == {
        "facts_as_of_after_computed_at",
        "sealed_before_computed_at",
    }
