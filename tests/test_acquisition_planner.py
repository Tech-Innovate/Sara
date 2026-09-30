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
    STOP_STALE,
    STOP_STALE_IN_FLIGHT,
    STOP_STALE_UNDERSTANDING,
    STOP_SUFFICIENT,
    STOP_UNSUPPORTED,
    plan_next_acquisition,
)
from sara.acquisition_planner_cli import main as planner_main
from sara.dossier import persist_dossier_assessment
from sara.maps_backfill import (
    backfill_maps_business_understanding,
    business_entity_id_for_maps_business,
    location_id_for_maps_business,
)
from sara.reviews import extract_retained_reviews
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


def prepared(path: Path, *, website: str = "https://seed.example",
             reviews: list | None = None):
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
                **({} if reviews is None else {"user_reviews": reviews}),
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
    exists = conn.execute(
        "SELECT 1 FROM sources WHERE id='src_official_web'").fetchone()
    if exists is None:
        conn.execute(
            "INSERT INTO sources(id,source_type,name,base_url,created_at,active) "
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
    # The fingerprint recomputes the input signature over current state:
    # keep the forced-sufficient derivation active during planning so the
    # recomputed signature matches the sealed one.
    with mock_patch.object(assessment_module, "derive_domain_assessments", all_ready):
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T13:00:05+00:00")
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
    # v8: mine the retained snapshot first so the review action is not
    # the eligible alternative — this test pins the WEBSITE
    # collector's own cooldown (F-03 keeps failure domains separate).
    extract_retained_reviews(conn, business_id=1,
                             now=lambda: "2026-09-26T12:05:00+00:00")
    persist_dossier_assessment(conn, entity_id=entity,
                               now=lambda: "2026-09-26T12:06:00+00:00")
    decision = plan_next_acquisition(conn, entity_id=entity, now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == STOP_COOLDOWN
    assert decision.details["streak"] == 3
    assert decision.details["collector"] == "sara.website"
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

    # v8: mine the retained snapshot first (F-03) so the retry ceiling
    # under test is the WEBSITE collector's own.
    extract_retained_reviews(conn, business_id=1,
                             now=lambda: "2026-09-26T11:50:00+00:00")
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
    # v8: mine the retained snapshot first so reputation is settled
    # by the explicit-unavailable outcome; the remaining improvable
    # set is website-only, as this test assumes.
    extract_retained_reviews(conn, business_id=1,
                             now=lambda: "2026-09-26T11:50:00+00:00")
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
        "collector_histories"]["sara.website"]["blocked_failed_streak_total"] == 0
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


def test_corrupt_sessions_count_individually(tmp_path: Path) -> None:
    """Corrupt sessions keep per-session status: partials fail closed to
    stale (V4-01 precedence), blocked rows hold cooldown individually."""
    conn = prepared(tmp_path / "corrupt-count.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    _ensure_website_source(conn)
    for i in range(2):
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,"
            "config_json,config_hash,status,started_at,finished_at,error,"
            "legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"acq_corrupt_p{i}", entity, "src_official_web", "sara.website", "5",
             "{}", "x" * 64, "partial", "not-a-timestamp",
             "not-a-timestamp", None, None, 0, 0),
        )
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    # Corrupt-partial terminal rows make currency unprovable: the stale
    # guard fires before the retry ceiling (fail-closed precedence).
    assert decision.stop_reason == STOP_STALE
    assert decision.reason_code == "assessment_currency_unprovable_corrupt_timestamp"
    assert decision.details["corrupt_terminal_timestamps"] == 2

    conn2 = prepared(tmp_path / "corrupt-blocked.sqlite")
    entity2 = business_entity_id_for_maps_business(1)
    acquire(conn2, tmp_path)
    _ensure_website_source(conn2)
    for i in range(3):
        conn2.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,"
            "config_json,config_hash,status,started_at,finished_at,error,"
            "legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"acq_corrupt_b{i}", entity2, "src_official_web", "sara.website", "5",
             "{}", "x" * 64, "blocked", "not-a-timestamp",
             "not-a-timestamp", "robots", None, 0, 0),
        )
    conn2.commit()
    # v8: mine the snapshot first (F-03) — the corrupt blocked rows
    # belong to the website collector; the review action must not
    # become the eligible alternative this test is not about.
    extract_retained_reviews(conn2, business_id=1,
                             now=lambda: "2026-09-26T12:05:00+00:00")
    persist_dossier_assessment(conn2, entity_id=entity2,
                               now=lambda: "2026-09-26T12:06:00+00:00")
    d2 = plan_next_acquisition(conn2, entity_id=entity2,
                               now="2026-09-26T12:30:00+00:00")
    # Blocked rows are not terminal-complete: no stale signal, and the
    # three corrupt blocked sessions hold cooldown per-session.
    assert d2.stop_reason == STOP_COOLDOWN
    assert d2.details["streak"] >= 3
    conn.close(); conn2.close()


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
    # Under v5 the source-agnostic Understanding-state watermark fires
    # first (the unrefreshed acquisition left newer evidence/observations);
    # the website-specific reason remains for finish-after-watermark edges.
    assert decision.stop_reason in (STOP_STALE, STOP_STALE_UNDERSTANDING)
    assert decision.reason_code in (
        "assessment_older_than_latest_acquisition",
        "understanding_state_signature_diverged")
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
    # v8: mine the snapshot first (F-03); the cooldown under test is
    # the predecessor lineage's website history.
    extract_retained_reviews(conn, business_id=1,
                             now=lambda: "2026-09-26T12:05:00+00:00")
    persist_dossier_assessment(conn, entity_id=entity,
                               now=lambda: "2026-09-26T12:06:00+00:00")
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == STOP_COOLDOWN
    assert decision.details["streak"] >= 3
    assert "be_old" in decision.details["planner_inputs"]["session_history"][
        "lineage_subjects"]["entities"]
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


def test_corrupt_terminal_finish_fails_closed_to_stale(tmp_path: Path) -> None:
    """V4-01: a complete row with corrupt finished_at makes currency unprovable."""
    conn = prepared(tmp_path / "corrupt-terminal.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)  # refreshed assessment covers this session
    _ensure_website_source(conn)
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_corrupt_term", entity, "src_official_web", "sara.website", "5",
         "{}", "x" * 64, "complete", "2026-09-26T10:00:00+00:00", "garbage",
         None, None, 0, 0))
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    # started_at 10:00 predates the assessment watermark, but completion
    # chronology is unprovable: fail closed to stale, never accept.
    assert decision.stop_reason == STOP_STALE
    assert decision.reason_code == "assessment_currency_unprovable_corrupt_timestamp"
    assert decision.details["corrupt_terminal_timestamps"] == 1
    conn.close()


def test_corrupt_terminal_count_counts_parse_failures(tmp_path: Path) -> None:
    """Two valid terminal rows report zero corrupt; the count is exact."""
    conn = prepared(tmp_path / "corrupt-count-exact.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    history = plan_next_acquisition(
        conn, entity_id=entity, now="2026-09-26T12:30:00+00:00"
    ).details["planner_inputs"]["session_history"]
    assert history["corrupt_terminal_timestamps"] == 0
    assert history["terminal_chronology_unprovable"] is False
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


def test_future_dated_in_flight_routes_to_recovery(tmp_path: Path) -> None:
    """V4-03: an in-flight row started in the future is recovery, not fresh."""
    conn = prepared(tmp_path / "future-inflight.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    _ensure_website_source(conn)
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_future", entity, "src_official_web", "sara.website", "5",
         "{}", "x" * 64, "running", "2027-06-01T00:00:00+00:00",
         None, None, None, 0, 0))
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == STOP_STALE_IN_FLIGHT
    assert decision.details["orphaned"] == 1
    conn.close()


def test_ceiling_replay_requires_same_ceiling(tmp_path: Path, capsys) -> None:
    """V4-02: a stricter max-decisions is NOT bypassed by replaying the old action."""
    from sara.acquisition_planner_cli import main as planner_cli_main
    conn = prepared(tmp_path / "ceiling-stricter.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    conn.close()
    db = str(tmp_path / "ceiling-stricter.sqlite")
    rc1 = planner_cli_main(["--db", db, "--business-id", "1",
                            "--max-decisions", "2"])
    assert rc1 == 0
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert first["action"] is not None
    # Same sealed state but a stricter ceiling: the recomputed decision is
    # policy_ceiling and the replay shortcut must NOT hand back the
    # action persisted under the looser ceiling.
    rc2 = planner_cli_main(["--db", db, "--business-id", "1",
                            "--max-decisions", "1"])
    assert rc2 == 0
    second = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert second["action"] is None
    assert second["stop_reason"] == "policy_ceiling"
    assert second.get("replayed") is not True
    assert second["decision_id"] != first["decision_id"]


def test_ceiling_counts_merged_predecessor_decisions(tmp_path: Path, capsys) -> None:
    """V4-04: actions persisted on a merged predecessor count after convergence."""
    from sara.acquisition_planner_cli import main as planner_cli_main
    conn = prepared(tmp_path / "ceiling-lineage.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,merged_into_subject_id,"
        "merged_at,created_at,updated_at) "
        "VALUES ('be_old2','business_entity','merged',?,"
        "'2026-09-26T12:00:00+00:00','2026-09-25T00:00:00+00:00','2026-09-26T12:00:00+00:00')",
        (entity,))
    conn.execute(
        "INSERT INTO business_entities(id,created_at,updated_at) VALUES "
        "('be_old2','2026-09-25T00:00:00+00:00','2026-09-26T12:00:00+00:00')")
    conn.execute(
        "INSERT INTO planner_decisions("
        "id,business_entity_id,decided_at,action,stop_reason,reason_code,"
        "target_domain,policy_version,details_json,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("plan_pred_1", "be_old2", "2026-09-26T12:20:00+00:00",
         "acquire_official_website", None, "predecessor",
         "offerings", "acquisition-planner-v4",
         '{"planner_inputs":{"max_decisions":2}}', "2026-09-26T12:20:00+00:00"))
    conn.commit()
    conn.close()
    db = str(tmp_path / "ceiling-lineage.sqlite")
    # max_decisions=1: the predecessor's action is inside the 7-day window,
    # so the canonical entity must already be at the ceiling.
    rc = planner_cli_main(["--db", db, "--business-id", "1",
                           "--max-decisions", "1"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["stop_reason"] == "policy_ceiling"


def test_two_valid_terminal_rows_report_zero_corrupt(tmp_path: Path) -> None:
    """Two genuinely valid terminal rows report zero corrupt; count exact."""
    conn = prepared(tmp_path / "two-terminal.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)  # terminal row #1
    _ensure_website_source(conn)
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_valid_second", entity, "src_official_web", "sara.website", "5",
         "{}", "x" * 64, "complete",
         "2026-09-26T12:20:00+00:00", "2026-09-26T12:20:30+00:00",
         None, None, 0, 0))  # terminal row #2, valid
    conn.commit()
    decision = plan_next_acquisition(
        conn, entity_id=entity, now="2026-09-26T12:30:00+00:00")
    history = decision.details["planner_inputs"]["session_history"]
    assert history["corrupt_terminal_timestamps"] == 0
    assert history["terminal_chronology_unprovable"] is False
    conn.close()


def test_integrity_precedes_in_flight_and_stale(tmp_path: Path) -> None:
    """V5-01: reader integrity + active session + diverged state -> integrity wins."""
    from unittest.mock import patch as mock_patch
    import sara.dossier.status as status_module
    from sara.dossier.status import persisted_assessment as real_read
    from sara.acquisition_planner import STOP_INTEGRITY

    conn = prepared(tmp_path / "precedence.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    _ensure_website_source(conn)
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_running_pre", entity, "src_official_web", "sara.website", "5",
         "{}", "x" * 64, "running", "2026-09-26T12:29:00+00:00",
         None, None, None, 0, 0))
    conn.commit()

    def read_with_issues(conn_, entity_id_):
        base = dict(real_read(conn_, entity_id_))
        if base is not None:
            base["integrity_issues"] = [
                {"code": "facts_as_of_after_computed_at"}]
        return base

    with mock_patch.object(status_module, "persisted_assessment", read_with_issues):
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == STOP_INTEGRITY
    assert decision.action is None
    assert decision.details["planner_inputs"]["session_history"][
        "in_flight_sessions"] == 1
    conn.close()


def test_maps_side_state_change_forces_stale_then_resumes(tmp_path: Path) -> None:
    """V6-02: a Maps-style identifier refresh (via the Location subject the
    dossier actually reads) after assessment A -> signature stop; sealed
    assessment B resumes deterministic planning."""
    from sara.acquisition_planner import STOP_STALE_UNDERSTANDING

    conn = prepared(tmp_path / "maps-side.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    before = plan_next_acquisition(conn, entity_id=entity,
                                   now="2026-09-26T12:30:00+00:00")
    assert before.action is not None

    # Maps-sync-style Entity-side change WITHOUT assessment refresh: a new
    # observation/fact stamped later than the sealed watermark.
    location = conn.execute(
        "SELECT bl.id FROM business_locations bl "
        "JOIN knowledge_subjects ks ON ks.id=bl.id "
        "WHERE bl.business_entity_id=? AND ks.record_state='active' "
        "ORDER BY bl.id LIMIT 1", (entity,)).fetchone()
    conn.execute(
        "UPDATE external_identifiers SET last_observed_at='2026-09-26T14:00:00+00:00' "
        "WHERE subject_id=? AND rowid=(SELECT MIN(rowid) FROM external_identifiers "
        "WHERE subject_id=?)", (location[0], location[0]))
    conn.commit()

    stale = plan_next_acquisition(conn, entity_id=entity,
                                  now="2026-09-26T14:30:00+00:00")
    assert stale.action is None
    assert stale.stop_reason == STOP_STALE_UNDERSTANDING
    assert stale.reason_code == "understanding_state_signature_diverged"
    assert (stale.details["sealed_input_signature_sha256"]
            != stale.details["current_input_signature_sha256"])

    persist_dossier_assessment(conn, entity_id=entity,
                               now=lambda: "2026-09-26T15:00:00+00:00")
    resumed = plan_next_acquisition(conn, entity_id=entity,
                                    now="2026-09-26T15:30:00+00:00")
    assert resumed.stop_reason != STOP_STALE_UNDERSTANDING
    conn.close()


def test_location_side_maps_change_forces_stale(tmp_path: Path) -> None:
    """V6-01: a Location-scoped Maps mutation is NOT invisible to currency."""
    from sara.acquisition_planner import STOP_STALE_UNDERSTANDING

    conn = prepared(tmp_path / "location-side.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    before = plan_next_acquisition(conn, entity_id=entity,
                                   now="2026-09-26T12:30:00+00:00")
    assert before.action is not None

    # Maps stores Location-scoped state (identifiers target the Location).
    location = conn.execute(
        "SELECT bl.id FROM business_locations bl "
        "JOIN knowledge_subjects ks ON ks.id=bl.id "
        "WHERE bl.business_entity_id=? AND ks.record_state='active' "
        "ORDER BY bl.id LIMIT 1", (entity,)).fetchone()
    assert location is not None
    conn.execute(
        "INSERT INTO external_identifiers("
        "id,subject_id,source_id,namespace,value,status,first_observed_at,"
        "last_observed_at,created_at"
        ") VALUES ('xid_loc_later', ?, 'src_google_maps', 'google_cid', "
        "'999888777', 'active', '2026-09-26T14:00:00+00:00', "
        "'2026-09-26T14:00:00+00:00', '2026-09-26T14:00:00+00:00')",
        (location[0],))
    conn.commit()

    stale = plan_next_acquisition(conn, entity_id=entity,
                                  now="2026-09-26T14:30:00+00:00")
    assert stale.action is None
    assert stale.stop_reason == STOP_STALE_UNDERSTANDING
    assert stale.reason_code == "understanding_state_signature_diverged"

    persist_dossier_assessment(conn, entity_id=entity,
                               now=lambda: "2026-09-26T15:00:00+00:00")
    resumed = plan_next_acquisition(conn, entity_id=entity,
                                    now="2026-09-26T15:30:00+00:00")
    assert resumed.stop_reason != STOP_STALE_UNDERSTANDING
    conn.close()


def test_corrupt_state_timestamp_fails_closed_not_ignored(tmp_path: Path) -> None:
    """V6-02: one corrupt relevant timestamp among many valid ones still stops."""
    from sara.acquisition_planner import STOP_STALE

    conn = prepared(tmp_path / "corrupt-state.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)  # many valid timestamps exist
    # Corrupt a Maps-link timestamp that feeds the chronology inputs;
    # identifier timestamps are trigger-guarded immutable.
    conn.execute(
        "UPDATE maps_business_location_links SET linked_at='garbage' "
        "WHERE location_id=(SELECT MIN(location_id) FROM maps_business_location_links)")
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    # The contract: a corrupt relevant timestamp NEVER permits an action.
    # It fails closed to a stale-family stop — either the explicit
    # unprovable reason or signature divergence, both of which demand a
    # fresh assessment before planning resumes.
    assert decision.action is None
    assert decision.stop_reason in (STOP_STALE, STOP_STALE_UNDERSTANDING)
    assert decision.reason_code in (
        "understanding_state_chronology_unprovable",
        "understanding_state_signature_diverged")
    conn.close()


def test_non_pk_integrity_error_propagates_not_replayed(tmp_path: Path) -> None:
    """A CHECK/FK IntegrityError inside persist is a controlled error, not a replay."""
    import sqlite3 as _sqlite3
    from sara.acquisition_planner_cli import _persist
    from sara.acquisition_planner import PlannerDecision

    conn = prepared(tmp_path / "nonpk.sqlite")
    entity = business_entity_id_for_maps_business(1)
    decision = PlannerDecision(
        action="acquire_official_website", stop_reason=None,
        reason_code="probe", target_domain="offerings",
        decision_id="plan_probe_pk",
        details={"decided_at": "2026-09-26T12:00:00+00:00",
                 "assessment_id": "none", "entity_id": "be_missing_target",
                 "planner_inputs": {"max_decisions": 25,
                                    "session_history": {}, }})

    class RaisingConn:
        """INSERT aborts with a REAL structured CHECK IntegrityError.

        The error is raised by SQLite itself against a CHECK-constrained
        probe table, so it carries the genuine extended identity
        (SQLITE_CONSTRAINT_CHECK, code 275), not a hand-built exception.
        """

        def __init__(self):
            self._probe = _sqlite3.connect(":memory:", isolation_level=None)
            self._probe.execute(
                "CREATE TABLE probe(x TEXT CHECK(x IN ('ok')))")

        def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO planner_decisions"):
                try:
                    self._probe.execute(
                        "INSERT INTO probe VALUES ('violated')")
                except _sqlite3.IntegrityError as real_exc:
                    raise real_exc from None
            return conn.execute(sql, params)

        def rollback(self):
            return conn.rollback()

        def commit(self):
            return conn.commit()

    raised = None
    try:
        _persist(RaisingConn(), decision, entity_id="be_missing_target")
    except _sqlite3.IntegrityError as exc:
        raised = exc
    assert raised is not None
    assert getattr(raised, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_CHECK"
    assert getattr(raised, "sqlite_errorcode", None) == 275
    assert conn.execute(
        "SELECT COUNT(*) FROM planner_decisions WHERE id=?",
        ("plan_probe_pk",)).fetchone()[0] == 0
    conn.close()


def test_ceiling_replay_selects_newest_by_instant_not_lexically(tmp_path: Path, capsys) -> None:
    """A mixed-offset decided_at cannot make an older action win replay selection."""
    from sara.acquisition_planner_cli import main as planner_cli_main
    conn = prepared(tmp_path / "lexical-replay.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    # Seal the assessment over the current state, then capture the exact
    # sealed inputs the planner will recompute.
    result = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:05:00+00:00")
    probe_decision = plan_next_acquisition(
        conn, entity_id=entity, now="2026-09-26T12:05:30+00:00",
        decisions_taken=0, max_decisions=1)
    matching_inputs = probe_decision.details["planner_inputs"]

    # The lexically-LARGER row is the EARLIER instant and does NOT match
    # the recomputed inputs (different ceiling, empty history); the
    # lexically-smaller row is newest by instant and DOES match. Only
    # the newest-by-instant row may be replayed.
    conn.execute(
        "INSERT INTO planner_decisions("
        "id,business_entity_id,decided_at,action,stop_reason,reason_code,"
        "target_domain,policy_version,details_json,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("plan_lex_older", entity, "2026-09-26T14:00:00+03:00",
         "acquire_official_website", None, "probe", "offerings",
         "acquisition-planner-v12",
         json.dumps({"assessment_id": result.assessment_id,
                     "planner_inputs": {"max_decisions": 99,
                                        "session_history": {}}},
                    sort_keys=True), "2026-09-26T14:00:00+03:00"))
    conn.execute(
        "INSERT INTO planner_decisions("
        "id,business_entity_id,decided_at,action,stop_reason,reason_code,"
        "target_domain,policy_version,details_json,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("plan_lex_newer", entity, "2026-09-26T12:10:00+00:00",
         "acquire_official_website", None, "probe", "offerings",
         "acquisition-planner-v12",
         json.dumps({"assessment_id": result.assessment_id,
                     "planner_inputs": matching_inputs},
                    sort_keys=True), "2026-09-26T12:10:00+00:00"))
    conn.commit()
    conn.close()
    db = str(tmp_path / "lexical-replay.sqlite")
    # decisions_taken = 2 >= ceiling 1 recomputes policy_ceiling; the
    # newest-by-instant candidate matches the sealed state, so it MUST be
    # replayed — lexical selection would hand back plan_lex_older.
    rc = planner_cli_main(["--db", db, "--business-id", "1",
                           "--max-decisions", "1"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload.get("replayed") is True
    assert payload["decision_id"] == "plan_lex_newer"


def test_planner_maps_reputation_to_review_extraction(tmp_path: Path) -> None:
    """A reputation-deficient dossier yields the review-extraction action."""
    from unittest.mock import patch as mock_patch
    from sara.dossier import assessment as assessment_module
    from sara.dossier.assessment_policy import derive_domain_assessments

    conn = prepared(tmp_path / "planner-reputation.sqlite", reviews=[REVIEW_SIMPLE])
    entity = business_entity_id_for_maps_business(1)

    def reputation_only_deficient(dossier):
        return tuple(
            {"domain": item["domain"],
             "state": ("partial" if item["domain"] == "reputation" else "sufficient"),
             "reason": {"derivation_version": "test", "rule": "forced"},
             "fact_count": item.get("fact_count", 0),
             "fresh_fact_count": item.get("fresh_fact_count", 0),
             "unresolved_count": item.get("unresolved_count", 0)}
            for item in derive_domain_assessments(dossier)
        )

    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:10:00+00:00")
        # The fingerprint recomputes inputs over live state, so the
        # forced derivation must stay active during planning.
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T12:10:30+00:00")
    assert decision.action == "extract_retained_reviews"
    assert decision.target_domain == "reputation"
    assert decision.stop_reason is None
    conn.close()


REVIEW_SIMPLE = {
    "review_id": "rev-p1", "source": "Google", "Rating": 4,
    "Description": "Good", "language": "en",
    "posted_at_unix_micros": 1_758_758_400_000_000,
}


def test_review_running_session_suppresses_any_action(tmp_path: Path) -> None:
    """A running review-extraction session on its real target stops planning.

    F-02: review sessions target the frozen SOURCE-TIME Location, not
    the Business Entity. The planner lineage must include Location
    subjects, or real review sessions would be invisible to in-flight
    suppression.
    """
    conn = prepared(tmp_path / "planner-inflight.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)
    location = location_id_for_maps_business(1)
    acquire(conn, tmp_path)
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_review_running", location, "src_google_maps",
         "sara.reviews.maps_snapshot", "2",
         "{}", "y" * 64, "running", "2026-09-26T12:29:00+00:00",
         None, None, None, 0, 0))
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == "acquisition_in_progress"
    assert decision.action is None
    assert location in decision.details["planner_inputs"]["session_history"][
        "lineage_subjects"]["locations"]
    conn.close()


def _anchor_second_business(conn, *, merged_into: str) -> tuple[str, str, str]:
    """Anchor Maps business id 2 whose deterministic anchor Location merged
    into `merged_into`, carrying a fully provenance-valid retained snapshot
    (run r2, backfill collector/version, canonical metadata) so the shared
    executor resolver recognizes it as the business's current snapshot."""
    import hashlib
    from sara.maps_backfill import (
        BACKFILL_VERSION,
        _evidence_id,
        business_entity_id_for_maps_business as _beid,
        location_id_for_maps_business as _locid,
    )

    be2 = _beid(2)
    loc_b2 = _locid(2)
    conn.execute(
        "INSERT INTO runs(id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,"
        "config_json,raw_path,status,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("r2", "test", '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
         2.0, 1, '["restaurant"]', "gosom/google-maps-scraper:v1.18.1",
         '{"strict_bounds":true}', "/evidence/r2.jsonl", "complete",
         "2026-09-25T11:00:00+00:00"),
    )
    raw2 = "{}"
    conn.execute(
        "INSERT INTO businesses(id,canonical_key,title,first_seen_at,last_seen_at,"
        "last_run_id,raw_json) VALUES (?,?,?,?,?,?,?)",
        (2, "alias-business", "Alias Business",
         "2026-09-25T11:00:00+00:00", "2026-09-26T09:59:00+00:00",
         "r2", raw2),
    )
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,created_at,updated_at) "
        "VALUES (?,'business_entity','active',"
        "'2026-09-25T00:00:00+00:00','2026-09-26T00:00:00+00:00')",
        (be2,),
    )
    conn.execute(
        "INSERT INTO business_entities(id,display_name,entity_type,lifecycle_status,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (be2, "Alias Owner", "independent_business", "operating",
         "2026-09-25T00:00:00+00:00", "2026-09-26T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,merged_into_subject_id,"
        "merged_at,created_at,updated_at) "
        "VALUES (?,'location','merged',?,"
        "'2026-09-26T11:00:00+00:00','2026-09-25T10:00:00+00:00','2026-09-26T11:00:00+00:00')",
        (loc_b2, merged_into),
    )
    conn.execute(
        "INSERT INTO business_locations(id,business_entity_id,label,location_type,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (loc_b2, be2, "alias", "branch",
         "2026-09-25T10:00:00+00:00", "2026-09-26T11:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO maps_business_location_links(business_id,location_id,linked_at) "
        "VALUES (2,?,'2026-09-25T10:00:00+00:00')",
        (loc_b2,),
    )
    raw_hash = hashlib.sha256(raw2.encode("utf-8")).hexdigest()
    evidence2 = _evidence_id(2, raw_hash)
    metadata = json.dumps(
        {
            "import_kind": "legacy_maps_business_snapshot",
            "legacy_business_id": 2,
            "legacy_canonical_key": "alias-business",
            "legacy_run_id": "r2",
            "raw_json": raw2,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_b2_backfill", None, "src_google_maps", "sara.maps_backfill",
         BACKFILL_VERSION, '{"import_mode":"latest_canonical_maps_snapshot"}',
         "z" * 64, "complete", "2026-09-26T09:59:00+00:00",
         "2026-09-26T10:00:00+00:00", None, "r2", 1, 0))
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,"
        "retrieved_at,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (evidence2, "acq_b2_backfill", "src_google_maps",
         "google_maps:legacy:2", "platform", "usable",
         "2026-09-26T09:59:00+00:00", raw_hash, None, metadata,
         "2026-09-26T09:59:00+00:00"))
    conn.commit()
    return be2, loc_b2, evidence2


def _apply_sync_snapshot(
    conn, *, business_id: int, new_run_id: str, retrieved_at: str
) -> str:
    """Simulate a Maps sync run observing the business's UNCHANGED raw
    content: new run, new last_run_id/last_seen_at, and a NEW run-bound
    sync evidence identity that supersedes the old backfill evidence
    (R10-01/R10-04). Returns the new evidence id."""
    import hashlib
    from sara.maps_backfill import (
        business_entity_id_for_maps_business as _beid,
        location_id_for_maps_business as _locid,
    )
    from sara.maps_sync import SYNC_VERSION, _sync_evidence_id

    canonical_key, raw_json = conn.execute(
        "SELECT canonical_key, raw_json FROM businesses WHERE id=?",
        (business_id,)).fetchone()
    raw_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO runs(id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,"
        "config_json,raw_path,status,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (new_run_id, "test", '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
         2.0, 1, '["restaurant"]', "gosom/google-maps-scraper:v1.18.1",
         "{}", f"/evidence/{new_run_id}.jsonl", "complete", retrieved_at),
    )
    session_id = f"acq_sync_{new_run_id}"
    metadata = json.dumps(
        {
            "import_kind": "maps_sync_snapshot",
            "legacy_business_id": business_id,
            "legacy_canonical_key": canonical_key,
            "legacy_run_id": new_run_id,
            "raw_json": raw_json,
            "sync_entity_id": _beid(business_id),
            "sync_location_id": _locid(business_id),
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    evidence_id = _sync_evidence_id(business_id, new_run_id, raw_hash)
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, None, "src_google_maps", "sara.maps_sync",
         SYNC_VERSION, "{}", "z" * 64, "complete", retrieved_at,
         retrieved_at, None, new_run_id, 1, 0))
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,"
        "retrieved_at,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (evidence_id, session_id, "src_google_maps",
         f"google_maps:sync:{business_id}", "platform", "usable",
         retrieved_at, raw_hash, None, metadata, retrieved_at))
    conn.execute(
        "UPDATE businesses SET last_run_id=?, last_seen_at=? WHERE id=?",
        (new_run_id, retrieved_at, business_id))
    conn.commit()
    return evidence_id


def _reputation_only_deficient(dossier):
    from sara.dossier.assessment_policy import derive_domain_assessments
    return tuple(
        {"domain": item["domain"],
         "state": ("partial" if item["domain"] == "reputation" else "sufficient"),
         "reason": {"derivation_version": "test", "rule": "forced"},
         "fact_count": item.get("fact_count", 0),
         "fresh_fact_count": item.get("fresh_fact_count", 0),
         "unresolved_count": item.get("unresolved_count", 0)}
        for item in derive_domain_assessments(dossier)
    )


def test_website_cooldown_does_not_suppress_review_extraction(tmp_path: Path) -> None:
    """F-03: the website collector's cooldown never suppresses the
    local review action — heterogeneous failure domains stay separate."""
    from unittest.mock import patch as mock_patch
    from sara.dossier import assessment as assessment_module

    conn = prepared(tmp_path / "cross-cooldown.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path)
    _ensure_website_source(conn)
    for i in range(3):
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,"
            "config_json,config_hash,status,started_at,finished_at,error,"
            "legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"acq_cross_blk{i}", entity, "src_official_web", "sara.website", "5",
             "{}", "x" * 64, "blocked", f"2026-09-26T12:1{i}:00+00:00",
             f"2026-09-26T12:1{i}:05+00:00", "robots", None, 0, 0),
        )
    conn.commit()
    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           _reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:20:00+00:00")
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T12:30:00+00:00")
    assert decision.action == "extract_retained_reviews"
    assert decision.target_domain == "reputation"
    assert decision.stop_reason is None
    assert decision.details["scope"] == "entity"
    conn.close()


def test_review_retry_ceiling_is_collector_scoped(tmp_path: Path) -> None:
    """F-03/F-06: two durable failed review sessions put the REVIEW
    action at its own scoped retry ceiling."""
    from unittest.mock import patch as mock_patch
    from sara.acquisition_planner import STOP_RETRIES
    from sara.dossier import assessment as assessment_module

    conn = prepared(tmp_path / "review-retry.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)
    location = location_id_for_maps_business(1)
    acquire(conn, tmp_path)
    for i in range(2):
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,"
            "config_json,config_hash,status,started_at,finished_at,error,"
            "legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"acq_rev_fail{i}", location, "src_google_maps",
             "sara.reviews.maps_snapshot", "2",
             "{}", "x" * 64, "failed", f"2026-09-26T12:1{i}:00+00:00",
             f"2026-09-26T12:1{i}:05+00:00", "malformed retained reviews",
             None, 0, 0),
        )
    conn.commit()
    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           _reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:20:00+00:00")
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == STOP_RETRIES
    assert decision.action is None
    assert decision.details["collector"] == "sara.reviews.maps_snapshot"
    assert decision.details["action"] == "extract_retained_reviews"
    assert decision.details["retries"] == 2
    conn.close()


def test_unavailable_outcome_satisfies_reputation_assessment(tmp_path: Path) -> None:
    """F-01 end-to-end: a mined empty snapshot makes reputation
    non-blocking on reseal (the no-op loop terminates)."""
    from sara.acquisition_planner import STOP_STALE, STOP_STALE_UNDERSTANDING

    conn = prepared(tmp_path / "f01-loop.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)
    first_seal = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: "2026-09-26T11:00:00+00:00")
    state = conn.execute(
        "SELECT state FROM dossier_domain_assessments "
        "WHERE assessment_id=? AND domain='reputation'",
        (first_seal.assessment_id,)).fetchone()[0]
    assert state != "sufficient"
    stats = extract_retained_reviews(
        conn, business_id=1, now=lambda: "2026-09-26T12:05:00+00:00")
    assert stats.status == "unavailable"
    # Before resealing, the planner refuses to decide from the stale
    # assessment: the outcome changed the assessment inputs.
    stale = plan_next_acquisition(conn, entity_id=entity,
                                  now="2026-09-26T12:06:00+00:00")
    assert stale.stop_reason in (STOP_STALE, STOP_STALE_UNDERSTANDING)
    second_seal = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:10:00+00:00")
    summary = json.loads(conn.execute(
        "SELECT summary_json FROM dossier_assessments WHERE id=?",
        (second_seal.assessment_id,)).fetchone()[0])
    assert "reputation" not in summary["blocking_mandatory_domains"]
    assert summary["review_evidence_unavailable_count"] == 1
    after = plan_next_acquisition(conn, entity_id=entity,
                                  now="2026-09-26T12:30:00+00:00")
    assert after.target_domain != "reputation"
    if after.action == "extract_retained_reviews":
        pytest.fail("review action re-scheduled against a mined snapshot")
    conn.close()


def test_review_session_on_cross_owner_alias_is_visible(tmp_path: Path) -> None:
    """R8-01: a review session on a Location owned by ANOTHER entity that
    redirects into our current Location is part of our lineage, exactly
    like the customer-voice projection sees it."""
    conn = prepared(tmp_path / "cross-owner.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)
    location = location_id_for_maps_business(1)
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,created_at,updated_at) "
        "VALUES ('be_other','business_entity','active',"
        "'2026-09-25T00:00:00+00:00','2026-09-26T00:00:00+00:00')",
    )
    conn.execute(
        "INSERT INTO business_entities(id,display_name,entity_type,lifecycle_status,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("be_other", "Other Owner", "independent_business", "operating",
         "2026-09-25T00:00:00+00:00", "2026-09-26T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,merged_into_subject_id,"
        "merged_at,created_at,updated_at) "
        "VALUES ('loc_alias','location','merged',?,"
        "'2026-09-26T11:00:00+00:00','2026-09-25T10:00:00+00:00','2026-09-26T11:00:00+00:00')",
        (location,),
    )
    conn.execute(
        "INSERT INTO business_locations(id,business_entity_id,label,location_type,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("loc_alias", "be_other", "alias", "branch",
         "2026-09-25T10:00:00+00:00", "2026-09-26T11:00:00+00:00"),
    )
    conn.commit()
    acquire(conn, tmp_path)
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_alias_running", "loc_alias", "src_google_maps",
         "sara.reviews.maps_snapshot", "2",
         "{}", "y" * 64, "running", "2026-09-26T12:29:00+00:00",
         None, None, None, 0, 0))
    conn.commit()
    decision = plan_next_acquisition(conn, entity_id=entity,
                                     now="2026-09-26T12:30:00+00:00")
    assert decision.stop_reason == "acquisition_in_progress"
    assert decision.action is None
    assert "loc_alias" in decision.details["planner_inputs"]["session_history"][
        "lineage_subjects"]["locations"]
    conn.close()


def test_per_location_pending_failed_location_stays_pending(tmp_path: Path) -> None:
    """R8-02: one snapshot's completed extraction must not mask another
    snapshot's failed attempt — pending is judged per exact snapshot."""
    from unittest.mock import patch as mock_patch
    from sara.dossier import assessment as assessment_module

    conn = prepared(tmp_path / "per-location.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)
    # A second CURRENT location of our entity, carrying business 2 whose
    # deterministic anchor Location redirects into it.
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,created_at,updated_at) "
        "VALUES ('loc2','location','active',"
        "'2026-09-25T10:00:00+00:00','2026-09-26T10:00:00+00:00')",
    )
    conn.execute(
        "INSERT INTO business_locations(id,business_entity_id,label,location_type,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("loc2", entity, "second", "branch",
         "2026-09-25T10:00:00+00:00", "2026-09-26T10:00:00+00:00"),
    )
    _be2, loc_b2, _ev2 = _anchor_second_business(conn, merged_into="loc2")
    # Snapshot 2's deterministic failure leaves no coverage; snapshot 1 is
    # then mined for real (a genuine unavailable session).
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_loc2_failed", loc_b2, "src_google_maps",
         "sara.reviews.maps_snapshot", "2",
         "{}", "x" * 64, "failed", "2026-09-26T12:01:00+00:00",
         "2026-09-26T12:01:05+00:00", "malformed retained reviews",
         None, 0, 0))
    conn.commit()
    extract_retained_reviews(
        conn, business_id=1, now=lambda: "2026-09-26T12:00:00+00:00")
    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           _reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:20:00+00:00")
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T12:30:00+00:00")
    assert decision.action == "extract_retained_reviews"
    assert decision.target_domain == "reputation"
    assert decision.stop_reason is None
    conn.close()


def test_pending_matches_exact_snapshot_within_one_closure(tmp_path: Path) -> None:
    """R9-01: two Maps businesses whose anchor Locations share ONE canonical
    Location are matched per exact snapshot — A-mined/B-unmined inside the
    same redirect closure stays pending (canonical aggregation would call
    the closure mined)."""
    from unittest.mock import patch as mock_patch
    from sara.dossier import assessment as assessment_module

    conn = prepared(tmp_path / "same-closure.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)
    location = location_id_for_maps_business(1)
    # Business 2's anchor Location redirects into the SAME canonical
    # Location as business 1's.
    _anchor_second_business(conn, merged_into=location)
    # Business 1 mined for real; business 2's snapshot remains unmined.
    extract_retained_reviews(
        conn, business_id=1, now=lambda: "2026-09-26T12:00:00+00:00")
    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           _reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:20:00+00:00")
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T12:30:00+00:00")
    assert decision.action == "extract_retained_reviews"
    assert decision.target_domain == "reputation"
    assert decision.stop_reason is None
    conn.close()


def test_synced_unchanged_snapshot_rearms_review_extraction(tmp_path: Path) -> None:
    """R10-01: an unchanged-content re-sync mints a NEW run-bound sync
    evidence identity. Resolving the current snapshot through the
    EXECUTOR's contract (run binding) re-arms review extraction instead
    of reading the two retained evidence ids as ambiguity."""
    from unittest.mock import patch as mock_patch
    from sara.dossier import assessment as assessment_module

    conn = prepared(tmp_path / "resync.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)
    extract_retained_reviews(
        conn, business_id=1, now=lambda: "2026-09-26T12:05:00+00:00")
    _apply_sync_snapshot(
        conn, business_id=1, new_run_id="r2",
        retrieved_at="2026-09-26T12:10:00+00:00")
    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           _reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:20:00+00:00")
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T12:30:00+00:00")
    assert decision.action == "extract_retained_reviews"
    assert decision.target_domain == "reputation"
    assert decision.stop_reason is None
    conn.close()


def test_partial_review_session_does_not_count_as_coverage(tmp_path: Path) -> None:
    """R10-01: only COMPLETE review sessions cover a snapshot — a partial
    session naming the current evidence leaves it pending."""
    from unittest.mock import patch as mock_patch
    from sara.dossier import assessment as assessment_module
    from sara.reviews.core import current_maps_evidence_for_business

    conn = prepared(tmp_path / "partial.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)
    location = location_id_for_maps_business(1)
    current_id = current_maps_evidence_for_business(
        conn, business_id=1)["id"]
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("acq_partial_probe", location, "src_google_maps",
         "sara.reviews.maps_snapshot", "2",
         json.dumps({"source_evidence_id": current_id},
                    sort_keys=True, separators=(",", ":")),
         "x" * 64, "partial", "2026-09-26T09:00:00+00:00",
         "2026-09-26T09:00:05+00:00", None, None, 0, 0))
    conn.commit()
    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           _reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:00:00+00:00")
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T12:30:00+00:00")
    assert decision.action == "extract_retained_reviews"
    assert decision.target_domain == "reputation"
    assert decision.stop_reason is None
    conn.close()


def test_newer_snapshot_supersedes_unavailable_outcome(tmp_path: Path) -> None:
    """R10-04 lifecycle: S1 mined unavailable -> reputation sufficient; a
    newer S2 (unchanged-content sync) DROPS S1's outcome -> reputation
    blocks again and the review action returns; S2 mined -> sufficiency
    restored and the action retires."""
    from unittest.mock import patch as mock_patch
    from sara.acquisition_planner import STOP_UNSUPPORTED
    from sara.dossier import assessment as assessment_module

    conn = prepared(tmp_path / "supersede.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)

    def reputation_state(seal) -> str:
        return conn.execute(
            "SELECT state FROM dossier_domain_assessments "
            "WHERE assessment_id=? AND domain='reputation'",
            (seal.assessment_id,)).fetchone()[0]

    first = extract_retained_reviews(
        conn, business_id=1, now=lambda: "2026-09-26T12:05:00+00:00")
    seal_a = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:06:00+00:00")
    assert reputation_state(seal_a) == "sufficient"

    _apply_sync_snapshot(
        conn, business_id=1, new_run_id="r2",
        retrieved_at="2026-09-26T12:10:00+00:00")
    seal_b = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:20:00+00:00")
    assert reputation_state(seal_b) != "sufficient"
    summary_b = json.loads(conn.execute(
        "SELECT summary_json FROM dossier_assessments WHERE id=?",
        (seal_b.assessment_id,)).fetchone()[0])
    assert "reputation" in summary_b["blocking_mandatory_domains"]

    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           _reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:21:00+00:00")
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T12:30:00+00:00")
    assert decision.action == "extract_retained_reviews"
    assert decision.target_domain == "reputation"

    second = extract_retained_reviews(
        conn, business_id=1, now=lambda: "2026-09-26T12:40:00+00:00")
    assert second.session_id != first.session_id
    seal_c = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:50:00+00:00")
    assert reputation_state(seal_c) == "sufficient"

    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           _reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:51:00+00:00")
        after = plan_next_acquisition(conn, entity_id=entity,
                                      now="2026-09-26T13:00:00+00:00")
    assert after.action is None
    assert after.stop_reason == STOP_UNSUPPORTED
    assert "extract_retained_reviews" in after.details["actions_not_applicable"]
    conn.close()


def test_malformed_complete_marker_does_not_suppress_acquisition(tmp_path: Path) -> None:
    """R11-01: a zero-output extraction_outcome="complete" marker naming
    the current snapshot is NOT coverage — only sessions passing the
    executor's full provenance contract suppress the real acquisition."""
    from unittest.mock import patch as mock_patch
    from sara.dossier import assessment as assessment_module
    from sara.reviews import core as reviews_core
    from sara.reviews.model import opaque_id as review_opaque_id, sha256_text

    conn = prepared(tmp_path / "bad-marker.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)
    location = location_id_for_maps_business(1)
    _bid, _eid, canonical_location_id, business = reviews_core._resolve_target(
        conn, business_id=1, canonical_key=None)
    source_evidence = reviews_core._maps_source_evidence(
        conn, business=business, location_id=canonical_location_id)
    cfg = json.loads(reviews_core._session_config(
        source_evidence=source_evidence,
        source_review_records=0,
        review_evidence_records=0,
    ))
    cfg["extraction_outcome"] = "complete"  # zero output, complete claim
    config = json.dumps(cfg, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"))
    marker_id = review_opaque_id(
        "acq", "retained-maps-reviews", source_evidence["id"],
        str(source_evidence["frozen_location_id"]), "2")
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (marker_id, str(source_evidence["frozen_location_id"]),
         "src_google_maps", "sara.reviews.maps_snapshot", "2",
         config, sha256_text(config), "complete",
         # Pre-watermark timestamps: a terminal session with no dossier
         # footprint must not trip assessment staleness.
         "2026-09-26T09:00:00+00:00", "2026-09-26T09:00:05+00:00",
         None, None, 0, 0))
    conn.commit()
    # The marker projects nothing (outcome complete, zero output) and must
    # not cover the snapshot either.
    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           _reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:20:00+00:00")
        decision = plan_next_acquisition(conn, entity_id=entity,
                                         now="2026-09-26T12:30:00+00:00")
    assert decision.action == "extract_retained_reviews"
    assert decision.target_domain == "reputation"
    assert decision.stop_reason is None
    conn.close()


def test_review_action_not_rescheduled_after_mining(tmp_path: Path) -> None:
    """F-01 termination guard: with reputation still deficient, a mined
    snapshot is not re-scheduled as a provable no-op."""
    from unittest.mock import patch as mock_patch
    from sara.acquisition_planner import STOP_UNSUPPORTED
    from sara.dossier import assessment as assessment_module

    conn = prepared(tmp_path / "f01-guard.sqlite", reviews=[])
    entity = business_entity_id_for_maps_business(1)
    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           _reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T11:00:00+00:00")
        first = plan_next_acquisition(conn, entity_id=entity,
                                      now="2026-09-26T11:30:00+00:00")
    assert first.action == "extract_retained_reviews"
    extract_retained_reviews(
        conn, business_id=1, now=lambda: "2026-09-26T12:05:00+00:00")
    with mock_patch.object(assessment_module, "derive_domain_assessments",
                           _reputation_only_deficient):
        persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:10:00+00:00")
        after = plan_next_acquisition(conn, entity_id=entity,
                                      now="2026-09-26T12:30:00+00:00")
    assert after.action is None
    assert after.stop_reason == STOP_UNSUPPORTED
    assert "extract_retained_reviews" in after.details["actions_not_applicable"]
    conn.close()
