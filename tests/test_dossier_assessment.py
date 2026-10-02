from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sara.dossier import (
    DossierAssessmentError,
    build_business_dossier,
    persist_dossier_assessment,
)
from sara.dossier.assessment_cli import main as assessment_main
from sara.maps_backfill import backfill_maps_business_understanding
from sara.migrations import MIGRATIONS, apply_migrations
from sara.reviews import extract_retained_reviews
from sara.storage import connect, connect_readonly, ingest_records
from sara.understanding_vocabulary import DOSSIER_DOMAIN_SEED_V1, seed_business_understanding_vocabulary


def _prepared(path: Path, *, reviews: bool = False, migrations=MIGRATIONS):
    conn = connect(path)
    assert apply_migrations(conn, migrations=migrations) == tuple(
        migration.version for migration in migrations
    )
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,'running',?)",
        (
            "r1",
            "assessment-test",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            2.0,
            1,
            '["restaurant"]',
            "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}',
            "/evidence/r1.jsonl",
            "2026-09-25T10:00:00+00:00",
        ),
    )
    conn.commit()
    record = {
        "place_id": "place-assessment",
        "cid": "cid-assessment",
        "data_id": "data-assessment",
        "title": "Assessment Restaurant",
        "category": "Restaurant",
        "address": "Assessment Street",
        "latitude": 21.55,
        "longitude": 39.18,
        "phone": "+966500000000",
        "website": "https://assessment.example",
        "review_rating": 4.6,
        "review_count": 91,
        "status": "Open",
        "link": "https://maps.example/assessment",
    }
    if reviews:
        record["user_reviews"] = [
            {
                "review_id": "review-assessment-1",
                "source": "Google",
                "Rating": 5,
                "Description": "Excellent service",
                "language": "en",
                "posted_at_unix_micros": 1_758_758_400_000_000,
                "Name": "Sensitive Reviewer Display Name",
                "ProfilePicture": "https://profiles.example/sensitive.jpg",
            }
        ]
    with patch("sara.storage.utc_now", return_value="2026-09-26T09:59:00+00:00"):
        ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))
    with patch("sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"):
        backfill_maps_business_understanding(conn)
    return conn


def _states(result) -> dict[str, str]:
    return {item["domain"]: item["state"] for item in result.domains}


def test_assessment_seals_complete_policy_snapshot_and_is_conservative(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "assessment.sqlite")
    result = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )

    states = _states(result)
    assert result.analysis_ready is False
    assert states["identity"] == "sufficient"
    assert states["classification"] == "sufficient"
    assert states["locations"] == "sufficient"
    assert states["communication"] == "sufficient"
    assert states["digital_presence"] == "sufficient"
    assert states["reputation"] == "partial"
    assert states["offerings"] == "not_started"
    assert states["business_model"] == "not_started"
    assert states["customer_journey"] == "partial"
    assert states["competitive_context"] == "not_started"
    assert states["provenance"] == "sufficient"
    assert states["unknowns"] == "sufficient"
    assert {"offerings", "business_model", "customer_journey", "reputation", "competitive_context"} <= set(
        result.blocking_mandatory_domains
    )

    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessment_seals").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM dossier_domain_assessments WHERE assessment_id=?",
        (result.assessment_id,),
    ).fetchone()[0] == len(DOSSIER_DOMAIN_SEED_V1)
    assert list(conn.execute("PRAGMA foreign_key_check")) == []
    conn.close()


def test_v1_non_active_external_identifier_migrates_without_fabricated_chronology(
    tmp_path: Path,
) -> None:
    conn = _prepared(
        tmp_path / "legacy-non-active-identifier.sqlite",
        migrations=MIGRATIONS[:1],
    )
    identifier_id = conn.execute(
        "SELECT id FROM external_identifiers WHERE status='active' ORDER BY id LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        "UPDATE external_identifiers SET status='retired' WHERE id=?",
        (identifier_id,),
    )
    conn.commit()

    assert apply_migrations(conn) == (2, 3, 4, 5)
    row = conn.execute(
        "SELECT status,status_changed_at FROM external_identifiers WHERE id=?",
        (identifier_id,),
    ).fetchone()
    assert tuple(row) == ("retired", None)

    result = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    assert result.assessment_id
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 1
    row = conn.execute(
        "SELECT status,status_changed_at FROM external_identifiers WHERE id=?",
        (identifier_id,),
    ).fetchone()
    assert tuple(row) == ("retired", None)
    conn.close()


def test_same_logical_state_reuses_deterministic_sealed_snapshot(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "idempotent.sqlite")
    first = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    second = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-29T10:00:00+00:00",
    )

    assert second.assessment_id == first.assessment_id
    assert second.facts_as_of == first.facts_as_of
    assert second.computed_at == first.computed_at
    assert second.already_assessed is True
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dossier_domain_assessments").fetchone()[0] == len(
        DOSSIER_DOMAIN_SEED_V1
    )
    conn.close()


def test_freshness_transition_creates_new_snapshot_without_rewriting_history(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "freshness.sqlite")
    fresh = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    stale = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2027-09-28T10:00:00+00:00",
    )

    assert stale.assessment_id != fresh.assessment_id
    assert stale.facts_as_of == fresh.facts_as_of
    assert _states(stale)["identity"] == "stale"
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessment_seals").fetchone()[0] == 2
    old = conn.execute(
        "SELECT analysis_ready,computed_at FROM dossier_assessments WHERE id=?",
        (fresh.assessment_id,),
    ).fetchone()
    assert tuple(old) == (0, "2026-09-28T10:00:00+00:00")
    conn.close()


def test_review_observations_project_as_customer_voice_without_profile_metadata(tmp_path: Path) -> None:
    db = tmp_path / "reviews.sqlite"
    conn = _prepared(db, reviews=True)
    extraction = extract_retained_reviews(
        conn,
        business_id=1,
        now=lambda: "2026-09-27T12:00:00+00:00",
    )
    assert extraction.observations_created == 1

    dossier = build_business_dossier(
        conn,
        business_id=1,
        evaluated_at="2026-09-28T10:00:00+00:00",
    )
    assert dossier["customer_voice"]["review_count"] == 1
    review = dossier["customer_voice"]["reviews"][0]
    assert review["normalized_value"]["review_id"] == "review-assessment-1"
    assert review["normalized_value"]["text_original"] == "Excellent service"
    assert "Sensitive Reviewer Display Name" not in json.dumps(
        dossier["customer_voice"], ensure_ascii=False, sort_keys=True
    )
    assert "profiles.example" not in json.dumps(
        dossier["customer_voice"], ensure_ascii=False, sort_keys=True
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM facts WHERE predicate='reputation.customer_review'"
    ).fetchone()[0] == 0

    assessment = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    assert _states(assessment)["reputation"] == "sufficient"
    assert assessment.analysis_ready is False
    conn.close()


def test_impossible_assessment_clock_rolls_back_all_writes(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "rollback.sqlite")
    with pytest.raises(DossierAssessmentError, match="later than the assessment clock"):
        persist_dossier_assessment(
            conn,
            business_id=1,
            now=lambda: "2020-01-01T00:00:00+00:00",
        )
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dossier_domain_assessments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessment_seals").fetchone()[0] == 0
    conn.close()


def test_assessment_cli_writes_only_explicit_assessment_state(tmp_path: Path, capsys) -> None:
    db = tmp_path / "cli.sqlite"
    conn = _prepared(db)
    before_maps = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    before_facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    conn.close()

    rc = assessment_main(["--db", str(db), "--business-id", "1"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["schema"] == "sara-dossier-assessment-result-v1"
    assert payload["analysis_ready"] is False
    assert payload["already_assessed"] is False

    check = connect(db)
    assert check.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == before_maps
    assert check.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == before_facts
    assert check.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 1
    check.close()


def test_readonly_dossier_surfaces_latest_sealed_assessment_after_writer(tmp_path: Path) -> None:
    db = tmp_path / "reader.sqlite"
    conn = _prepared(db)
    written = persist_dossier_assessment(
        conn,
        business_id=1,
        now=lambda: "2026-09-28T10:00:00+00:00",
    )
    conn.close()

    ro = connect_readonly(db)
    dossier = build_business_dossier(
        ro,
        business_id=1,
        evaluated_at="2026-09-28T11:00:00+00:00",
    )
    ro.close()
    persisted = dossier["dossier_status"]["persisted_current_policy"]
    assert persisted["id"] == written.assessment_id
    assert persisted["analysis_ready"] is False
    assert len(persisted["domains"]) == len(DOSSIER_DOMAIN_SEED_V1)


def test_assessment_on_v1_only_schema_fails_closed(tmp_path: Path) -> None:
    """A database migrated only through v1 refuses assessment cleanly."""
    conn = _prepared(
        tmp_path / "assessment-v1-only.sqlite",
        migrations=MIGRATIONS[:1],
    )
    try:
        with pytest.raises(DossierAssessmentError, match="schema v2"):
            persist_dossier_assessment(
                conn,
                business_id=1,
                now=lambda: "2026-09-28T10:00:00+00:00",
            )
        assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 0
        assert not conn.in_transaction
    finally:
        conn.close()


def test_dossier_reader_on_v1_only_schema_fails_closed(tmp_path: Path) -> None:
    """The read-only dossier surface refuses a v1-only database cleanly."""
    from sara.dossier import build_business_dossier
    from sara.dossier.core import DossierQueryError

    conn = _prepared(
        tmp_path / "reader-v1-only.sqlite",
        migrations=MIGRATIONS[:1],
    )
    try:
        with pytest.raises(DossierQueryError, match="schema v2"):
            build_business_dossier(conn, business_id=1)
        assert not conn.in_transaction
    finally:
        conn.close()

# ---- v4 provenance supersession (invalid historical website sessions) ----

MENU_SITE = {
    "https://assessment.example/": """
        <link rel="canonical" href="https://assessment.example/">
        <a href="/menu">Menu</a>
    """,
    "https://assessment.example/menu": "<h1>Signature dishes</h1>",
}


def _acquire(conn, tmp_path):
    from sara.website import CrawlConfig, collect_official_website
    from sara.website.http import HttpResponse, WebsiteFetchError

    class FakeClient:
        def __init__(self, site_pages):
            self.site_pages = site_pages

        def fetch(self, url: str) -> HttpResponse:
            if url not in self.site_pages:
                raise WebsiteFetchError(f"HTTP 404 for {url}")
            return HttpResponse(
                requested_url=url, final_url=url, status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=self.site_pages[url].encode(),
                media_type="text/html", charset="utf-8",
            )

    client = FakeClient(MENU_SITE)
    return collect_official_website(
        conn, evidence_root=tmp_path / "ev", business_id=1,
        config=CrawlConfig(page_limit=8),
        now=lambda: "2026-09-28T11:00:00+00:00",
        client_factory=lambda **_kw: client, refresh_assessment=False,
    )


def _forge_legacy_unsealed_session(conn, entity, started_at):
    """Insert a pre-v5 historical website session at schema v4 (running
    -> finalization WITHOUT a seal, exactly how production's 30 invalid
    sessions were born), returning (session_id, evidence_id)."""
    from sara.website.model import (
        COLLECTOR_VERSION,
        canonical_json as wcanonical,
        opaque_id as wopaque,
        sha256_text as wsha,
    )

    if conn.execute(
        "SELECT 1 FROM sources WHERE id='src_official_web'"
    ).fetchone() is None:
        conn.execute(
            "INSERT INTO sources(id,source_type,name,base_url,created_at,"
            "active) VALUES ('src_official_web','official_web',"
            "'Official website',NULL,'2026-01-01T00:00:00+00:00',1)")
    config = wcanonical(
        {
            "entity_id": entity,
            "start_url": "https://legacy.example/",
            "page_limit": 8,
            "depth_limit": 2,
            "max_response_bytes": 1048576,
            "timeout_seconds": 10.0,
            "request_interval_seconds": 1.0,
            "max_policy_delay_seconds": 30.0,
            "retry_attempt_limit": 4,
            "retry_base_delay_seconds": 1.0,
            "retry_max_delay_seconds": 30.0,
            "retry_delay_budget_seconds": 60.0,
            "user_agent": "SaraBusinessUnderstanding/1.0",
            "obey_robots": True,
            "evidence_root": "/tmp/legacy-ev",
        }
    )
    config_hash = wsha(config)
    session_id = wopaque("acq", entity, started_at, config_hash)
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,'running',?,NULL,NULL,NULL,0,0)",
        (session_id, entity, "src_official_web", "sara.website",
         COLLECTOR_VERSION, config, config_hash, started_at))
    content = "<html>legacy home</html>"
    content_sha = wsha(content)
    evidence_id = wopaque("ev", session_id, "https://legacy.example/",
                          content_sha)
    metadata = wcanonical(
        {
            "acquisition_kind": "bounded_official_website",
            "entity_id": entity,
            "start_url": "https://legacy.example/",
            "requested_url": "https://legacy.example/",
            "final_url": "https://legacy.example/",
            "crawl_depth": 0,
            "page_role": "home",
            "home_page": True,
            "business_wide_scope_eligible": True,
            "crawl_frontier_exhausted": True,
            "title": "Legacy",
            "canonical_url": "https://legacy.example/",
            "channels": [],
            "observation_ids": [],
        }
    )
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,"
        "status,retrieved_at,published_at,language,media_type,"
        "content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,?,'usable',?,NULL,NULL,'text/html',?,NULL,?,?)",
        (evidence_id, session_id, "src_official_web",
         "https://legacy.example/", "official", started_at, content_sha,
         metadata, started_at))
    # pre-v5 finalization: terminal WITHOUT a seal (impossible under v5)
    conn.execute(
        "UPDATE acquisition_sessions SET status='complete', finished_at=?, "
        "error=NULL, evidence_count=1, observation_count=0 WHERE id=? "
        "AND status='running'", (started_at, session_id))
    conn.commit()
    return session_id, evidence_id


def _entity_of(conn):
    from sara.maps_backfill import business_entity_id_for_maps_business
    return business_entity_id_for_maps_business(1)


# 1. superseded invalid session + independent valid support => sufficient
def test_provenance_superseded_invalid_session_is_sufficient(
        tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "prov-super.sqlite",
                     migrations=MIGRATIONS[:4])
    entity = _entity_of(conn)
    legacy_sid, legacy_eid = _forge_legacy_unsealed_session(
        conn, entity, "2026-09-27T10:00:00+00:00")
    assert apply_migrations(conn) == (5,)
    seed_business_understanding_vocabulary(conn)
    _acquire(conn, tmp_path)

    dossier = build_business_dossier(
        conn, business_id=1, evaluated_at="2026-09-28T11:30:00+00:00")
    issue_codes = {str(i["code"]) for i in dossier["integrity_issues"]}
    assert "customer_journey_website_session_invalid" in issue_codes
    journey_evidence = {
        str(entry.get("evidence_id"))
        for stage in dossier["customer_journey"]["stages"]
        for entry in stage.get("evidence", ())
    }
    assert legacy_eid not in journey_evidence

    seal = persist_dossier_assessment(
        conn, business_id=1, now=lambda: "2026-09-28T11:30:00+00:00")
    row = conn.execute(
        "SELECT state, reason_json FROM dossier_domain_assessments "
        "WHERE assessment_id=? AND domain='provenance'",
        (seal.assessment_id,)).fetchone()
    assert row[0] == "sufficient"
    reason = json.loads(row[1])
    assert reason["code"] == "material_traces_with_superseded_invalid_history"
    assert reason["superseded_invalid_sessions"] == [legacy_sid]
    assert "provenance" not in seal.blocking_mandatory_domains
    conn.close()


# 2. material fact supported ONLY by the invalid session => insufficient
def test_provenance_invalid_only_support_stays_insufficient(
        tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "prov-dep.sqlite",
                     migrations=MIGRATIONS[:4])
    entity = _entity_of(conn)
    legacy_sid, legacy_eid = _forge_legacy_unsealed_session(
        conn, entity, "2026-09-27T10:00:00+00:00")
    stamp = "2026-09-27T10:00:00+00:00"
    conn.execute(
        "INSERT INTO observations("
        "id,subject_id,predicate,evidence_id,value_json,"
        "normalized_value_json,value_hash,observation_kind,observed_at,"
        "extracted_at,extraction_method,extractor_name,extractor_version,"
        "confidence,created_at"
        ") VALUES ('obs_legacy_dep',?,?,?, 'true', 'true', ?, "
        "'detected_capability', ?, ?, 'heuristic', 'sara.website', '5', "
        "0.8, ?)",
        (entity, "capability.whatsapp", legacy_eid,
         "a" * 64, stamp, stamp, stamp))
    conn.execute(
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,"
        "normalized_value_json,value_hash,status,valid_from,valid_to,"
        "last_verified_at,reconciled_at,reconciliation_version,created_at"
        ") VALUES ('fact_legacy_dep',?, 'capability.whatsapp', "
        "'__single__', 'true', 'true', ?, 'single_source', ?, NULL, ?, ?, "
        "'legacy-test', ?)",
        (entity, "b" * 64, stamp, stamp, stamp, stamp))
    conn.execute(
        "INSERT INTO fact_observation_support(fact_id,observation_id,"
        "support_role) VALUES ('fact_legacy_dep','obs_legacy_dep',"
        "'supports')")
    conn.commit()
    assert apply_migrations(conn) == (5,)
    seed_business_understanding_vocabulary(conn)

    dossier = build_business_dossier(
        conn, business_id=1, evaluated_at="2026-09-28T11:30:00+00:00")
    assert any(
        str(i["code"]) == "customer_journey_website_session_invalid"
        for i in dossier["integrity_issues"])
    seal = persist_dossier_assessment(
        conn, business_id=1, now=lambda: "2026-09-28T11:30:00+00:00")
    row = conn.execute(
        "SELECT state, reason_json FROM dossier_domain_assessments "
        "WHERE assessment_id=? AND domain='provenance'",
        (seal.assessment_id,)).fetchone()
    assert row[0] == "insufficient"
    reason = json.loads(row[1])
    assert reason["code"] == "provenance_integrity_issues_present"
    assert reason["material_items_dependent_on_invalid_sessions"] == [
        {"kind": "fact", "id": "fact_legacy_dep",
         "predicate": "capability.whatsapp"}]
    assert "provenance" in seal.blocking_mandatory_domains
    conn.close()


# 3. old-policy snapshots never surface under the v3 policy identity
def test_v2_snapshots_not_current_under_v3_policy(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "prov-policy.sqlite")
    with patch("sara.dossier.assessment.DOSSIER_POLICY_VERSION",
               "business-understanding-v2"):
        persist_dossier_assessment(
            conn, business_id=1, now=lambda: "2026-09-28T11:00:00+00:00")
    dossier = build_business_dossier(
        conn, business_id=1, evaluated_at="2026-09-28T11:30:00+00:00")
    assert dossier["dossier_status"]["persisted_current_policy"] is None
    seal = persist_dossier_assessment(
        conn, business_id=1, now=lambda: "2026-09-28T11:10:00+00:00")
    current = build_business_dossier(
        conn, business_id=1,
        evaluated_at="2026-09-28T11:30:00+00:00"
    )["dossier_status"]["persisted_current_policy"]
    assert current is not None
    assert current["id"] == seal.assessment_id
    assert current["policy_version"] == "business-understanding-v3"
    assert current["summary"]["derivation_version"] == (
        "dossier-assessment-v4")
    conn.close()


# 4. deterministic identity under the new derivation
def test_v4_deterministic_assessment_identity(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "prov-det.sqlite")
    first = persist_dossier_assessment(
        conn, business_id=1, now=lambda: "2026-09-28T11:00:00+00:00")
    second = persist_dossier_assessment(
        conn, business_id=1, now=lambda: "2026-09-28T11:05:00+00:00")
    assert second.assessment_id == first.assessment_id
    assert second.already_assessed
    conn.close()
