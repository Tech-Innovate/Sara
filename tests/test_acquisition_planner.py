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
    assert apply_migrations(conn) == (1, 2, 3)
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
    # The decision clock is a sealed input: a later clock is a visibly
    # different decision (its cooldown/retry windows moved), never a silent
    # divergence.
    d3 = plan_next_acquisition(conn, entity_id=entity, now="2026-09-27T09:00:00+00:00")
    assert d3.decision_id != d1.decision_id
    assert (d1.action, d1.reason_code, d1.target_domain) == (d3.action, d3.reason_code, d3.target_domain)
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
    assert current_schema_version(conn2) == 3
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
    assert d1.decision_id != d2.decision_id
    assert d1.decision_id != d3.decision_id
    # The inputs are visible in the persisted snapshot.
    assert d1.details["planner_inputs"]["decisions_taken"] == 0
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
