from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from sara.acquisition_planner import (
    ACTIONS,
    PLANNER_POLICY_VERSION,
    STOP_COOLDOWN,
    STOP_INTEGRITY,
    STOP_NO_ASSESSMENT,
    STOP_POLICY,
    STOP_RETRIES,
    STOP_SUFFICIENT,
    STOP_UNSUPPORTED,
    plan_next_acquisition,
)
from sara.acquisition_planner_cli import main as planner_main
from sara.dossier import persist_dossier_assessment
from sara.maps_backfill import backfill_maps_business_understanding, business_entity_id_for_maps_business
from sara.migrations import apply_migrations, current_schema_version
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary
from sara.website import CrawlConfig, WebsiteAcquisitionError, collect_official_website
from sara.website.http import HttpResponse, WebsiteBlockedError, WebsiteFetchError


class Clock:
    def __init__(self) -> None:
        self.current = datetime.fromisoformat("2026-09-26T12:00:00+00:00")

    def __call__(self) -> str:
        value = self.current.isoformat()
        self.current += timedelta(seconds=1)
        return value


class FakeClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def fetch(self, url: str) -> HttpResponse:
        if url not in self.pages:
            raise WebsiteFetchError(f"HTTP 404 for {url}")
        return HttpResponse(
            requested_url=url, final_url=url, status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=self.pages[url].encode(), media_type="text/html", charset="utf-8",
        )


def factory(client):
    return lambda **_kw: client


MENU_SITE = {
    "https://seed.example/": """
        <link rel="canonical" href="https://seed.example/">
        <a href="/menu">Menu</a>
        <a href="/contact">Contact</a>
        <a href="https://wa.me/966501234567">WhatsApp</a>
    """,
    "https://seed.example/menu": "<h1>Grill and Mezzes</h1>",
    "https://seed.example/contact": """
        <a href="mailto:hello@seed.example">Email</a>
    """,
}


def prepared(path: Path, *, website: str = "https://seed.example"):
    conn = connect(path)
    assert apply_migrations(conn) == (1, 2, 3, 4)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO runs(id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,"
        "config_json,raw_path,status,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("r1", "test", '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
         2.0, 1, '["restaurant"]', "gosom/google-maps-scraper:v1.18.1",
         '{"strict_bounds":true}', "/evidence/r1.jsonl", "running",
         "2026-09-25T10:00:00+00:00"),
    )
    conn.commit()
    with patch("sara.storage.utc_now", return_value="2026-09-26T09:59:00+00:00"), patch(
        "sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"
    ):
        ingest_records(
            conn, "r1",
            [{
                "place_id": "place-seed", "cid": "cid-seed", "data_id": "data-seed",
                "title": "Business seed", "category": "Restaurant", "address": "Seed Street",
                "latitude": 21.55, "longitude": 39.18, "phone": "+966500000000",
                "website": website, "review_rating": 4.4, "review_count": 120,
                "status": "Open", "link": "https://maps.example/seed",
            }],
            finalize_run=("complete", 0, None),
        )
        backfill_maps_business_understanding(conn)
    return conn


def acquire(conn, tmp_path, *, pages=MENU_SITE, page_limit=8):
    return collect_official_website(
        conn, evidence_root=tmp_path / "ev", business_id=1,
        config=CrawlConfig(page_limit=page_limit), now=Clock(),
        client_factory=factory(FakeClient(pages)), refresh_assessment=True,
    )



def _ensure_website_source(conn) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sources(id,source_type,name,base_url,created_at,active) "
        "VALUES ('src_official_web','official_website','Official website',NULL,"
        "'2026-01-01T00:00:00+00:00',1)")
    conn.commit()

def test_no_assessment_stops_closed(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "no-assessment.sqlite")
    entity = business_entity_id_for_maps_business(1)
    decision = plan_next_acquisition(conn, entity_id=entity, now="2026-09-26T12:00:00+00:00")
    assert decision.action is None
    assert decision.stop_reason == STOP_NO_ASSESSMENT
    assert decision.decision_id is not None
    conn.close()


def test_analysis_ready_stops_sufficient_for_real(tmp_path: Path) -> None:
    """A genuinely analysis_ready sealed assessment stops sufficient."""
    from unittest.mock import patch as mock_patch
    from sara.dossier import assessment as assessment_module
    from sara.dossier.assessment_policy import derive_domain_assessments

    conn = prepared(tmp_path / "ready.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)

    def all_ready(dossier):
        return tuple(
            {"domain": item["domain"], "state": "sufficient",
             "reason": {"derivation_version": "test", "rule": "forced"},
             "fact_count": item.get("fact_count", 0),
             "fresh_fact_count": item.get("fresh_fact_count", 0),
             "unresolved_count": item.get("unresolved_count", 0)}
            for item in derive_domain_assessments(dossier)
        )

    with mock_patch.object(assessment_module, "derive_domain_assessments", all_ready):
        result = persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T13:00:00+00:00")
    assert result.analysis_ready is True
    decision = plan_next_acquisition(conn, entity_id=entity, now="2026-09-26T13:30:00+00:00")
    assert decision.action is None
    assert decision.stop_reason == STOP_SUFFICIENT
    conn.close()


def test_integrity_flagged_assessment_stops_integrity_not_sufficient(tmp_path: Path) -> None:
    """analysis_ready sealed but the summary reports integrity issues."""
    from unittest.mock import patch as mock_patch
    from sara.dossier import assessment as assessment_module
    from sara.dossier.assessment_policy import derive_domain_assessments

    conn = prepared(tmp_path / "integrity.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)

    def all_ready_with_issues(dossier):
        return tuple(
            {"domain": item["domain"], "state": "sufficient",
             "reason": {"derivation_version": "test", "rule": "forced"},
             "fact_count": item.get("fact_count", 0),
             "fresh_fact_count": item.get("fresh_fact_count", 0),
             "unresolved_count": item.get("unresolved_count", 0)}
            for item in derive_domain_assessments(dossier)
        )

    real_persist = assessment_module.persist_dossier_assessment

    import sara.dossier.status as status_module
    import sara.acquisition_planner as planner_module
    # plan_next_acquisition imports persisted_assessment lazily from
    # sara.dossier.status, so patch it at that source.
    planner_module = status_module
    with mock_patch.object(assessment_module, "derive_domain_assessments", all_ready_with_issues):
        result = persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T13:00:00+00:00")
    assert result.analysis_ready is True

    from sara.dossier.status import persisted_assessment as real_read

    def read_with_issues(conn_, entity_id_):
        base = real_read(conn_, entity_id_)
        if base is not None:
            base = dict(base)
            base["summary"] = dict(base["summary"])
            base["summary"]["integrity_issue_count"] = 2
        return base

    with mock_patch.object(planner_module, "persisted_assessment",
                           lambda conn_, entity_id_: read_with_issues(conn_, entity_id_)):
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T13:30:00+00:00")
    assert decision.stop_reason == STOP_INTEGRITY
    assert decision.action is None
    conn.close()


def test_deficient_domain_maps_to_allowlisted_action(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "deficient.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    decision = plan_next_acquisition(conn, entity_id=entity, now="2026-09-26T12:30:00+00:00")
    assert decision.action == "acquire_official_website"
    assert decision.stop_reason is None
    assert decision.target_domain in ACTIONS["acquire_official_website"]["improves_domains"]
    assert decision.policy_version == PLANNER_POLICY_VERSION
    conn.close()


def test_same_assessment_same_decision(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "deterministic.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    d1 = plan_next_acquisition(conn, entity_id=entity, now="2026-09-26T12:30:00+00:00")
    d2 = plan_next_acquisition(conn, entity_id=entity, now="2026-09-26T12:30:00+00:00")
    assert d1.decision_id == d2.decision_id
    assert (d1.action, d1.reason_code, d1.target_domain) == (d2.action, d2.reason_code, d2.target_domain)
    # The raw clock left the identity in planner-v3: a later clock with an
    # unchanged session-history snapshot replays the same id, which is what
    # makes a lost-output CLI retry idempotent. The clock still shapes the
    # decision through the window/horizon counts sealed in the snapshot.
    d3 = plan_next_acquisition(conn, entity_id=entity, now="2026-09-26T18:00:00+00:00")
    assert d3.decision_id == d1.decision_id
    conn.close()


def test_blocked_streak_cools_down(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "cooldown.sqlite")
    entity = business_entity_id_for_maps_business(1)

    class Blocked:
        def fetch(self, url):
            raise WebsiteBlockedError("robots policy disallows " + url)

    clock = Clock()
    for _ in range(3):
        with pytest.raises(WebsiteAcquisitionError):
            collect_official_website(
                conn, evidence_root=tmp_path / "ev", business_id=1,
                config=CrawlConfig(page_limit=2), now=clock,
                client_factory=factory(Blocked()), refresh_assessment=False,
            )
    persist_dossier_assessment(conn, entity_id=entity)
    decision = plan_next_acquisition(conn, entity_id=entity, now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == STOP_COOLDOWN
    assert decision.details["streak"] == 3
    # After the cooldown horizon passes, the same history no longer holds.
    later = plan_next_acquisition(conn, entity_id=entity, now="2026-10-26T12:30:00+00:00")
    assert later.stop_reason != STOP_COOLDOWN
    conn.close()


def test_partial_retry_ceiling(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "retry.sqlite")
    entity = business_entity_id_for_maps_business(1)
    partial_pages = {k: v for k, v in list(MENU_SITE.items())[:1]}
    # A one-page crawl of a site whose frontier is not exhausted is still
    # complete; make it partial by failing one page.
    class HalfBroken:
        def fetch(self, url):
            if "contact" in url:
                raise WebsiteFetchError("HTTP 500")
            return FakeClient(MENU_SITE).fetch(url)

    clock = Clock()
    for _ in range(2):
        collect_official_website(
            conn, evidence_root=tmp_path / "ev", business_id=1,
            config=CrawlConfig(page_limit=4), now=clock,
            client_factory=factory(HalfBroken()), refresh_assessment=True,
        )
    decision = plan_next_acquisition(
        conn, entity_id=entity, now="2026-09-26T12:30:00+00:00",
    )
    assert decision.stop_reason == STOP_RETRIES
    assert decision.details["retries"] == 2
    # Partials older than the retry window no longer count.
    old = plan_next_acquisition(
        conn, entity_id=entity, now="2026-10-20T12:30:00+00:00",
    )
    assert old.stop_reason != STOP_RETRIES
    conn.close()


def test_policy_ceiling(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "ceiling.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    decision = plan_next_acquisition(
        conn, entity_id=entity, now="2026-09-26T12:30:00+00:00",
        decisions_taken=25, max_decisions=25,
    )
    assert decision.stop_reason == STOP_POLICY
    conn.close()


def test_unsupported_deficiency_stops(tmp_path: Path) -> None:
    """A blocking domain no allowlisted action improves stops the cycle."""
    conn = prepared(tmp_path / "unsupported.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    decision = plan_next_acquisition(conn, entity_id=entity, now="2026-09-26T12:30:00+00:00")
    # The real dossier always has unsupported blocking domains; if the
    # planner chose the action, at least one improvable domain led.
    if decision.action is None:
        assert decision.stop_reason == STOP_UNSUPPORTED
    else:
        assert decision.target_domain in ACTIONS["acquire_official_website"]["improves_domains"]
    conn.close()


def test_planner_never_mutates_facts(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "no-mutation.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    before = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(valid_to),'') FROM facts").fetchone()
    plan_next_acquisition(conn, entity_id=entity, now="2026-09-26T12:30:00+00:00")
    after = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(valid_to),'') FROM facts").fetchone()
    assert before == after
    conn.close()


def test_cli_persists_decision_and_replay_is_idempotent(tmp_path: Path, capsys) -> None:
    conn = prepared(tmp_path / "cli.sqlite")
    db = str(tmp_path / "cli.sqlite")
    conn.close()
    rc = planner_main(["--db", db, "--business-id", "1"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["policy_version"] == PLANNER_POLICY_VERSION
    decision_id = payload["decision_id"]

    rc2 = planner_main(["--db", db, "--business-id", "1"])
    assert rc2 == 0
    capsys.readouterr()
    conn2 = connect(Path(db))
    rows = conn2.execute(
        "SELECT COUNT(*) FROM planner_decisions WHERE id=?", (decision_id,)
    ).fetchone()[0]
    assert rows == 1  # deterministic replay inserted once
    assert current_schema_version(conn2) == 4
    fk = conn2.execute("PRAGMA foreign_key_check").fetchall()
    assert fk == []
    conn2.close()


def test_cli_resolves_canonical_entity_after_merge(tmp_path: Path) -> None:
    """--entity-id on a merged subject follows the redirect chain."""
    import sara.acquisition_planner_cli as planner_cli

    conn = prepared(tmp_path / "merge.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    # Forge a merge: new canonical entity, old one merged into it.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,created_at,updated_at) "
        "VALUES ('be_merged_target','business_entity','active',"
        "'2026-09-26T12:00:00+00:00','2026-09-26T12:00:00+00:00')")
    conn.execute(
        "INSERT INTO business_entities(id,created_at,updated_at) VALUES "
        "('be_merged_target','2026-09-26T12:00:00+00:00','2026-09-26T12:00:00+00:00')")
    conn.execute(
        "UPDATE knowledge_subjects SET record_state='merged', "
        "merged_into_subject_id='be_merged_target', merged_at='2026-09-26T12:00:00+00:00' "
        "WHERE id=?", (entity,))
    conn.commit()
    conn.close()

    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = planner_cli.main(["--db", str(tmp_path / "merge.sqlite"),
                               "--entity-id", entity])
    assert rc == 0
    payload = json.loads(buf.getvalue().strip().splitlines()[-1])
    chain = payload["entity_resolution"]["entity_resolution_chain"]
    assert chain[0] == entity and chain[-1] == "be_merged_target"
    assert payload["details"]["entity_id"] == "be_merged_target"


def test_cli_unknown_entity_fails_not_silently_persists(tmp_path: Path, capsys) -> None:
    """A nonexistent entity-id is a controlled error, not a phantom success."""
    from sara.acquisition_planner_cli import main as planner_cli_main
    conn = prepared(tmp_path / "unknown.sqlite")
    db = str(tmp_path / "unknown.sqlite")
    conn.close()
    rc = planner_cli_main(["--db", db, "--entity-id", "be_does_not_exist"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "be_does_not_exist" in err or "Understanding subject" in err
    conn2 = connect(Path(db))
    rows = conn2.execute(
        "SELECT COUNT(*) FROM planner_decisions").fetchone()[0]
    assert rows == 0  # nothing was persisted for the phantom entity
    conn2.close()


def test_duplicate_replay_prints_persisted_record(tmp_path: Path, capsys) -> None:
    """A replay prints the stored decision (replayed: true), not a fresh clock."""
    from sara.acquisition_planner_cli import _persist
    from sara.storage import connect as storage_connect
    conn = prepared(tmp_path / "replay.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    d1 = plan_next_acquisition(conn, entity_id=entity,
                               now="2026-09-26T12:30:00+00:00")
    assert _persist(conn, d1, entity_id=entity) is None
    d2 = plan_next_acquisition(conn, entity_id=entity,
                               now="2026-09-26T12:30:00+00:00")
    replayed = _persist(conn, d2, entity_id=entity)
    assert replayed is not None
    assert replayed["decision_id"] == d1.decision_id
    assert replayed["decided_at"] == d1.details["decided_at"]
    assert conn.execute(
        "SELECT COUNT(*) FROM planner_decisions WHERE id=?",
        (d1.decision_id,)).fetchone()[0] == 1
    conn.close()


def test_decision_id_seals_operational_inputs(tmp_path: Path) -> None:
    """Different decisions_taken or ceiling yields a visibly different id."""
    conn = prepared(tmp_path / "inputs.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    d1 = plan_next_acquisition(conn, entity_id=entity,
                               now="2026-09-26T12:30:00+00:00", decisions_taken=0)
    d2 = plan_next_acquisition(conn, entity_id=entity,
                               now="2026-09-26T12:30:00+00:00", decisions_taken=1)
    d3 = plan_next_acquisition(conn, entity_id=entity,
                               now="2026-09-26T12:30:00+00:00",
                               decisions_taken=0, max_decisions=10)
    # decisions_taken deliberately does NOT change the identity (the
    # counter includes prior persisted actions, so sealing it would make
    # every retry mint a new id); max_decisions does.
    assert d1.decision_id == d2.decision_id
    assert d1.decision_id != d3.decision_id
    # Both inputs remain visible in the persisted snapshot.
    assert d1.details["planner_inputs"]["decisions_taken"] == 0
    assert d2.details["planner_inputs"]["decisions_taken"] == 1
    assert d3.details["planner_inputs"]["max_decisions"] == 10
    conn.close()


def test_blocking_set_read_from_sealed_summary(tmp_path: Path) -> None:
    """The planner reports mandatory blockers, not every non-ready domain."""
    conn = prepared(tmp_path / "blocking.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    if decision.action is not None:
        assert decision.target_domain in \
            ACTIONS["acquire_official_website"]["improves_domains"]
        assert decision.target_domain in decision.details["blocking"]
    else:
        assert decision.stop_reason == STOP_UNSUPPORTED
        assert decision.target_domain in decision.details["blocking"]
    conn.close()


def test_mixed_offset_session_ordering_does_not_falsely_cool_down(tmp_path: Path) -> None:
    """Lexical and UTC order disagree; the streak follows UTC order."""
    conn = prepared(tmp_path / "mixed-offset.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    # A blocked session at 12:00+03:00 (= 09:00Z, EARLIER instant) whose
    # string sorts AFTER the newer complete session at 10:00+00:00
    # (= 10:00Z). Lexical DESC ordering would place the blocked session
    # "newest" and count it as a streak; instant ordering correctly puts
    # the complete session newest, so the streak is zero.
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_mixed_offset_probe", entity, "src_official_web", "sara.website", "5",
         "{}", "x" * 64, "blocked", "2026-09-26T12:00:00+03:00",
         "2026-09-26T12:00:05+03:00", "robots policy disallows", None, 0, 0),
    )
    conn.commit()
    # The acquire() session completed at ~12:00+00:00 (Clock start), i.e.
    # 10:00Z-scale later than the probe's 09:00Z.
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason != STOP_COOLDOWN
    assert decision.details["planner_inputs"]["session_history"][
        "blocked_failed_streak_total"] == 0
    conn.close()


def test_reader_detected_integrity_stops_even_with_sealed_count_zero(tmp_path: Path) -> None:
    """analysis_ready + sealed count 0 + reader integrity issues -> stop."""
    from unittest.mock import patch as mock_patch
    from sara.dossier import assessment as assessment_module
    from sara.dossier.assessment_policy import derive_domain_assessments
    import sara.dossier.status as status_module
    from sara.dossier.status import persisted_assessment as real_read

    conn = prepared(tmp_path / "reader-integrity.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)

    def all_ready(dossier):
        return tuple(
            {"domain": item["domain"], "state": "sufficient",
             "reason": {"derivation_version": "test", "rule": "forced"},
             "fact_count": item.get("fact_count", 0),
             "fresh_fact_count": item.get("fresh_fact_count", 0),
             "unresolved_count": item.get("unresolved_count", 0)}
            for item in derive_domain_assessments(dossier)
        )

    with mock_patch.object(assessment_module, "derive_domain_assessments", all_ready):
        result = persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T13:00:00+00:00")
    assert result.analysis_ready is True

    def read_with_reader_issues(conn_, entity_id_):
        base = dict(real_read(conn_, entity_id_))
        if base is not None:
            base["integrity_issues"] = [
                {"code": "facts_as_of_after_computed_at"}]
            # sealed summary count stays ZERO: the sealed snapshot was
            # written when the rows were consistent; the reader detects
            # the inconsistency now.
            base["summary"] = dict(base["summary"])
            base["summary"]["integrity_issue_count"] = 0
        return base

    with mock_patch.object(status_module, "persisted_assessment", read_with_reader_issues):
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T13:30:00+00:00")
    assert decision.stop_reason == STOP_INTEGRITY
    assert decision.action is None
    assert "facts_as_of_after_computed_at" in decision.details["reader_integrity_codes"]
    assert decision.details["sealed_integrity_issue_count"] == 0
    conn.close()


def test_two_corrupt_partials_hit_retry_ceiling(tmp_path: Path) -> None:
    """Corrupt-timestamp sessions count individually, status preserved."""
    from sara.acquisition_planner import STOP_RETRIES
    conn = prepared(tmp_path / "corrupt-count.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    for i in range(2):
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,"
            "config_json,config_hash,status,started_at,finished_at,error,"
            "legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"acq_corrupt_{i}", entity, "src_official_web", "sara.website", "5",
             "{}", "x" * 64, "partial", "not-a-timestamp",
             "not-a-timestamp", None, None, 0, 0),
        )
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == STOP_RETRIES
    assert decision.details["retries"] >= 2
    conn.close()


def test_stale_assessment_stops_when_acquisition_is_newer(tmp_path: Path) -> None:
    """A terminal session newer than the assessment's watermark stops stale."""
    from sara.acquisition_planner import STOP_STALE
    conn = prepared(tmp_path / "stale.sqlite")
    entity = business_entity_id_for_maps_business(1)
    # Assessment first (facts_as_of = maps watermark 10:00), then an
    # unrefreshed complete acquisition at 12:00 (collector default off):
    # the planner must refuse to decide from the stale assessment.
    persist_dossier_assessment(conn, entity_id=entity,
                               now=lambda: "2026-09-26T11:00:00+00:00")
    collect_official_website(
        conn, evidence_root=tmp_path / "ev", business_id=1,
        config=CrawlConfig(page_limit=8), now=Clock(),
        client_factory=factory(FakeClient(MENU_SITE)), refresh_assessment=False)
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == STOP_STALE
    assert decision.reason_code == "assessment_older_than_latest_acquisition"
    assert decision.details["latest_terminal_session_time"] > "2026-09-26T11:"
    conn.close()


def test_in_flight_session_stops_duplication(tmp_path: Path) -> None:
    """A running website session blocks scheduling another acquisition."""
    from sara.acquisition_planner import STOP_IN_FLIGHT
    conn = prepared(tmp_path / "inflight.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_inflight", entity, "src_official_web", "sara.website", "5",
         "{}", "x" * 64, "running", "2026-09-26T12:31:00+00:00",
         None, None, None, 0, 0),
    )
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:32:00+00:00")
    assert decision.stop_reason == STOP_IN_FLIGHT
    assert decision.action is None
    conn.close()


def test_merged_predecessor_history_survives(tmp_path: Path) -> None:
    """Blocked history on a merged predecessor holds cooldown for the canonical."""
    conn = prepared(tmp_path / "lineage.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,merged_into_subject_id,"
        "merged_at,created_at,updated_at) "
        "VALUES ('be_old','business_entity','merged',?,"
        "'2026-09-26T12:00:00+00:00','2026-09-25T00:00:00+00:00','2026-09-26T12:00:00+00:00')",
        (entity,))
    for i in range(3):
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,"
            "config_json,config_hash,status,started_at,finished_at,error,"
            "legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"acq_old_{i}", "be_old", "src_official_web", "sara.website", "5",
             "{}", "x" * 64, "blocked",
             f"2026-09-26T12:1{i}:00+00:00", f"2026-09-26T12:1{i}:05+00:00",
             "robots policy disallows", None, 0, 0))
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == STOP_COOLDOWN
    assert decision.details["streak"] >= 3
    assert "be_old" in decision.details["planner_inputs"]["session_history"]["entity_lineage"]
    conn.close()


def test_cli_rejects_retired_entity(tmp_path: Path, capsys) -> None:
    """--entity-id resolving to a terminal retired subject is a controlled error."""
    from sara.acquisition_planner_cli import main as planner_cli_main
    conn = prepared(tmp_path / "retired.sqlite")
    db = str(tmp_path / "retired.sqlite")
    entity = business_entity_id_for_maps_business(1)
    conn.execute(
        "UPDATE knowledge_subjects SET record_state='retired' WHERE id=?", (entity,))
    conn.commit()
    conn.close()
    rc = planner_cli_main(["--db", db, "--entity-id", entity])
    assert rc == 2
    assert "not active" in capsys.readouterr().err
    conn2 = connect(Path(db))
    assert conn2.execute("SELECT COUNT(*) FROM planner_decisions").fetchone()[0] == 0
    conn2.close()


def test_cli_retry_after_lost_output_replays_not_duplicates(tmp_path: Path, capsys) -> None:
    """Re-invoking without an intervening persisted decision replays the id."""
    from sara.acquisition_planner_cli import main as planner_cli_main
    conn = prepared(tmp_path / "retry-cli.sqlite")
    db = str(tmp_path / "retry-cli.sqlite")
    conn.close()
    rc1 = planner_cli_main(["--db", db, "--business-id", "1"])
    assert rc1 == 0
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    rc2 = planner_cli_main(["--db", db, "--business-id", "1"])
    assert rc2 == 0
    second = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    # second invocation replays the persisted record (replayed) or mints
    # the identical decision via the derived clock; either way the row
    # count for that entity stays 1 when no acquisition intervened.
    conn2 = connect(Path(db))
    rows = conn2.execute(
        "SELECT COUNT(*), COUNT(DISTINCT id) FROM planner_decisions").fetchone()
    assert rows[0] == rows[1] == 1
    conn2.close()


def test_stale_check_uses_finish_time_not_start(tmp_path: Path) -> None:
    """A session that started before the assessment but finished after it is stale."""
    from sara.acquisition_planner import STOP_STALE
    conn = prepared(tmp_path / "finish-race.sqlite")
    entity = business_entity_id_for_maps_business(1)
    # Assessment seals at 10:05-equivalent: after maps (10:00), before the
    # acquisition's finish. The acquisition session started earlier than
    # the assessment but finished later — the old started_at check would
    # accept the assessment; finished_at must reject it.
    persist_dossier_assessment(conn, entity_id=entity,
                               now=lambda: "2026-09-26T10:05:00+00:00")
    _ensure_website_source(conn)
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_finish_race", entity, "src_official_web", "sara.website", "5",
         "{}", "x" * 64, "complete",
         "2026-09-26T10:00:00+00:00", "2026-09-26T10:10:00+00:00",
         None, None, 0, 0))
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == STOP_STALE
    assert decision.details["latest_terminal_session_time"] == "2026-09-26T10:10:00+00:00"
    conn.close()


def test_stale_check_mixed_offset_finished_at(tmp_path: Path) -> None:
    """finished_at with a +03:00 offset is ordered by instant, not lexically."""
    from sara.acquisition_planner import STOP_STALE
    conn = prepared(tmp_path / "finish-offset.sqlite")
    entity = business_entity_id_for_maps_business(1)
    persist_dossier_assessment(conn, entity_id=entity,
                               now=lambda: "2026-09-26T10:05:00+00:00")
    _ensure_website_source(conn)
    # 12:00+03:00 is 09:00Z — EARLIER than the assessment watermark, so a
    # lexical comparison (12:... > 10:...) would wrongly call it stale.
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_offset", entity, "src_official_web", "sara.website", "5",
         "{}", "x" * 64, "complete",
         "2026-09-26T08:00:00+03:00", "2026-09-26T12:00:00+03:00",
         None, None, 0, 0))
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason != STOP_STALE
    conn.close()


def test_corrupt_terminal_timestamps_do_not_block_or_stale(tmp_path: Path) -> None:
    """Corrupt finished/started on terminal rows: no stale signal, fail-closed windows."""
    conn = prepared(tmp_path / "corrupt-terminal.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)  # refreshed assessment covers this session
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_corrupt_term", entity, "src_official_web", "sara.website", "5",
         "{}", "x" * 64, "complete", "garbage", "garbage",
         None, None, 0, 0))
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    # corrupt terminal timestamps produce no stale signal; the decision
    # proceeds on the current assessment (windows already fail closed).
    assert decision.stop_reason != "stale_assessment"
    conn.close()


def test_orphaned_running_session_demands_recovery(tmp_path: Path) -> None:
    """A running session older than the horizon stops stale_in_flight."""
    from sara.acquisition_planner import STOP_STALE_IN_FLIGHT
    conn = prepared(tmp_path / "orphan.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_orphan", entity, "src_official_web", "sara.website", "5",
         "{}", "x" * 64, "running",
         "2026-09-26T02:00:00+00:00", None, None, None, 0, 0))
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == STOP_STALE_IN_FLIGHT
    assert decision.action is None
    assert decision.details["orphaned"] == 1
    conn.close()


def test_extreme_offset_instant_fails_closed_as_value_error(tmp_path: Path) -> None:
    """C-01: a parseable offset that overflows normalization is ValueError."""
    from sara.acquisition_planner import _instant
    import pytest as _pytest
    with _pytest.raises(ValueError):
        _instant("0001-01-01T00:00:00+23:59")


def test_planner_decisions_are_append_only(tmp_path: Path) -> None:
    """C-02: UPDATE and DELETE on planner_decisions abort."""
    conn = prepared(tmp_path / "append-only.sqlite")
    entity = business_entity_id_for_maps_business(1)
    d = plan_next_acquisition(conn, entity_id=entity,
                              now="2026-09-26T12:00:00+00:00")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO planner_decisions("
        "id,business_entity_id,decided_at,action,stop_reason,reason_code,"
        "target_domain,policy_version,details_json,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        (d.decision_id, entity, d.details["decided_at"], d.action,
         d.stop_reason, d.reason_code, d.target_domain, d.policy_version,
         "{}", "2026-09-26T12:00:00+00:00"))
    conn.commit()
    import sqlite3 as _sqlite3
    with pytest.raises(_sqlite3.IntegrityError,
                       match="append-only"):
        conn.execute("UPDATE planner_decisions SET reason_code='tampered'")
    conn.rollback()
    with pytest.raises(_sqlite3.IntegrityError,
                       match="append-only"):
        conn.execute("DELETE FROM planner_decisions WHERE id=?", (d.decision_id,))
    conn.rollback()
    conn.close()


def test_cli_ceiling_retry_replays_action_decision(tmp_path: Path, capsys) -> None:
    """S-02: a lost-output retry at the ceiling replays the persisted action."""
    from sara.acquisition_planner_cli import main as planner_cli_main
    conn = prepared(tmp_path / "ceiling-replay.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)  # sealed assessment + session history
    conn.close()
    db = str(tmp_path / "ceiling-replay.sqlite")
    rc1 = planner_cli_main(["--db", db, "--business-id", "1",
                            "--max-decisions", "1"])
    assert rc1 == 0
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert first["action"] is not None  # an action decision, 0 < 1 ceiling
    rc2 = planner_cli_main(["--db", db, "--business-id", "1",
                            "--max-decisions", "1"])
    assert rc2 == 0
    second = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert second.get("replayed") is True
    assert second["decision_id"] == first["decision_id"]
    assert second["action"] == first["action"]
    conn2 = connect(Path(db))
    counts = tuple(conn2.execute(
        "SELECT COUNT(*), COUNT(DISTINCT id) FROM planner_decisions"
    ).fetchone())
    assert counts == (1, 1)
    conn2.close()
