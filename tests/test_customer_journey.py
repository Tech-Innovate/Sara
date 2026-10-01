from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from sara.acquisition_planner import UNSUPPORTED_DOMAINS
from sara.dossier import persist_dossier_assessment
from sara.dossier.assessment import understanding_state_fingerprint
from sara.dossier.assessment_policy import derive_domain_assessments
from sara.dossier.customer_journey import (
    RECONSTRUCTION_VERSION,
    reconstruct_customer_journey,
)
from sara.dossier.surface import build_business_dossier
from sara.maps_backfill import backfill_maps_business_understanding, business_entity_id_for_maps_business
from sara.migrations import apply_migrations
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary
from sara.website import CrawlConfig, collect_official_website
from sara.website.http import HttpResponse, WebsiteFetchError

sys.path.insert(0, "tests")


def _fake_client_factory(pages):
    class FakeClient:
        def __init__(self, site_pages) -> None:
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

    client = FakeClient(pages)
    return lambda **_kw: client


class Clock:
    def __init__(self) -> None:
        self.current = datetime.fromisoformat("2026-09-26T12:00:00+00:00")

    def __call__(self) -> str:
        value = self.current.isoformat()
        self.current += timedelta(seconds=1)
        return value


def prepared(path: Path, *, reviews=None, website="https://seed.example/",
             phone="+966500000000"):
    conn = connect(path)
    assert apply_migrations(conn) == (1, 2, 3, 4, 5)
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
    record = {
        "place_id": "place-journey", "cid": "cid-journey", "data_id": "data-journey",
        "title": "Journey Business", "category": "Restaurant",
        "address": "Journey Street", "latitude": 21.55, "longitude": 39.18,
        "review_rating": 4.4, "review_count": 12, "status": "Open",
        "link": "https://maps.example/journey",
    }
    if website is not None:
        record["website"] = website
    if phone is not None:
        record["phone"] = phone
    if reviews is not None:
        record["user_reviews"] = reviews
    with patch("sara.storage.utc_now", return_value="2026-09-26T09:59:00+00:00"), patch(
        "sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"
    ):
        ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))
        backfill_maps_business_understanding(conn)
    return conn


def acquire(conn, tmp_path, *, pages, page_limit=8, clock=None,
            client_factory=None):
    return collect_official_website(
        conn, evidence_root=tmp_path / "ev", business_id=1,
        config=CrawlConfig(page_limit=page_limit), now=clock or Clock(),
        client_factory=client_factory or _fake_client_factory(pages),
        refresh_assessment=True,
    )


BOOKING_SITE = {
    "https://seed.example/": """
        <link rel="canonical" href="https://seed.example/">
        <a href="/menu">Menu</a>
        <a href="/contact">Contact</a>
        <a href="https://booksy.com/some-salon">Book Now</a>
        <a href="https://wa.me/966501234567">WhatsApp</a>
    """,
    "https://seed.example/menu": "<h1>Grill and Mezzes</h1>",
    "https://seed.example/contact": """
        <a href="mailto:hello@seed.example">Email</a>
    """,
}

ORDERING_SITE = {
    "https://seed.example/": """
        <link rel="canonical" href="https://seed.example/">
        <a href="/menu">Menu</a>
        <a href="https://hungerstation.com/some-store">Order online</a>
        <a href="https://wa.me/966501234567">WhatsApp us</a>
    """,
    "https://seed.example/menu": "<h1>Grill and Mezzes</h1>",
}

SUPPORT_SITE = {
    "https://seed.example/": """
        <link rel="canonical" href="https://seed.example/">
        <a href="/menu">Menu</a>
        <a href="/support">Support</a>
        <a href="https://helpdesk.example/answers">Help Center</a>
    """,
    "https://seed.example/menu": "<h1>Grill and Mezzes</h1>",
    "https://seed.example/support": "<h1>How can we help?</h1>",
}

MENU_ONLY_SITE = {
    "https://seed.example/": """
        <link rel="canonical" href="https://seed.example/">
        <a href="/menu">Menu</a>
    """,
    "https://seed.example/menu": "<h1>Grill and Mezzes</h1>",
}

REVIEW = {
    "review_id": "rev-j1", "source": "Google", "Rating": 5,
    "Description": "I paid online and the delivery arrived cold, then I "
                   "reordered and subscribed to their loyalty program",
    "language": "en", "posted_at_unix_micros": 1_758_758_400_000_000,
}


def _journey(conn, entity, evaluated_at="2026-09-26T12:30:00+00:00"):
    dossier = build_business_dossier(conn, entity_id=entity, evaluated_at=evaluated_at)
    return dossier, dossier["customer_journey"]


def _states(journey):
    return {stage["stage"]: stage["evidence_state"] for stage in journey["stages"]}


# 1. verified website + offerings + booking link
def test_full_site_reconstructs_entry_evaluation_and_booking(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "full.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=BOOKING_SITE)
    dossier, journey = _journey(conn, entity)
    states = _states(journey)
    assert states["discover"] == "observed"
    assert states["evaluate"] == "observed"
    assert states["contact"] == "observed"
    assert states["book_order"] == "observed"
    assert states["pay"] == "unknown"
    # Every observed stage carries exact evidence entries.
    for stage_name in ("discover", "evaluate", "contact", "book_order"):
        stage = next(s for s in journey["stages"] if s["stage"] == stage_name)
        assert stage["evidence"], stage_name
        for entry in stage["evidence"]:
            assert entry["evidence_id"]
            assert entry["source_id"] in {"src_google_maps", "src_official_web"}
            assert entry["content_sha256"]
            assert entry["acquisition_session_id"]
            assert entry["collector_name"]
            assert entry["collector_version"]
    booking = next(s for s in journey["stages"] if s["stage"] == "book_order")
    assert any(c["channel_type"] == "booking" for c in booking["channels"])
    # Maps listing -> official website hand-off is directly observed.
    assert {"from": "maps_listing", "to": "official_website"} in [
        {k: h[k] for k in ("from", "to")} for h in journey["handoffs"]
    ]
    conn.close()


# 2. ordering/WhatsApp reconstruct only the supported stages
def test_ordering_whatsapp_reconstruct_only_supported_stages(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "ordering.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=ORDERING_SITE)
    _, journey = _journey(conn, entity)
    states = _states(journey)
    assert states["book_order"] == "observed"
    assert states["contact"] == "observed"
    assert states["evaluate"] == "observed"
    assert states["support"] == "unknown"
    assert {"from": "official_website", "to": "ordering"} in [
        {k: h[k] for k in ("from", "to")} for h in journey["handoffs"]
    ]
    conn.close()


# 3. positive evidence from a partial crawl is usable
def test_partial_crawl_positive_evidence_usable(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "partial.sqlite")
    entity = business_entity_id_for_maps_business(1)

    pages = dict(BOOKING_SITE)

    class HalfBroken:
        def fetch(self, url):
            if "menu" in url:
                raise WebsiteFetchError("HTTP 500")
            if url not in pages:
                raise WebsiteFetchError(f"HTTP 404 for {url}")
            return HttpResponse(
                requested_url=url, final_url=url, status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=pages[url].encode(), media_type="text/html", charset="utf-8",
            )

    stats = collect_official_website(
        conn, evidence_root=tmp_path / "ev", business_id=1,
        config=CrawlConfig(page_limit=8), now=Clock(),
        client_factory=lambda **_kw: HalfBroken(), refresh_assessment=False,
    )
    assert stats.status == "partial"
    _, journey = _journey(conn, entity)
    states = _states(journey)
    assert states["book_order"] == "observed"
    assert states["discover"] == "observed"
    conn.close()


# 4. a partial crawl that did not find booking does not produce bounded not_observed
def test_partial_crawl_without_booking_is_unknown_not_bounded(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "partial-noabsence.sqlite")
    entity = business_entity_id_for_maps_business(1)

    class HalfBroken:
        def fetch(self, url):
            if "menu" in url:
                raise WebsiteFetchError("HTTP 500")
            page = {
                "https://seed.example/":
                    '<link rel="canonical" href="https://seed.example/">'
                    '<a href="/menu">Menu</a>',
            }.get(url)
            if page is None:
                raise WebsiteFetchError(f"HTTP 404 for {url}")
            return HttpResponse(
                requested_url=url, final_url=url, status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=page.encode(), media_type="text/html", charset="utf-8",
            )

    stats = collect_official_website(
        conn, evidence_root=tmp_path / "ev", business_id=1,
        config=CrawlConfig(page_limit=8), now=Clock(),
        client_factory=lambda **_kw: HalfBroken(), refresh_assessment=False,
    )
    assert stats.status == "partial"
    _, journey = _journey(conn, entity)
    book_order = next(s for s in journey["stages"] if s["stage"] == "book_order")
    assert book_order["evidence_state"] == "unknown"
    conn.close()


# 5. frontier-exhausted bounded inspection represents not-observed without absence
def test_exhausted_crawl_bounded_not_observed_without_absence(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "bounded.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = acquire(conn, tmp_path, pages=MENU_ONLY_SITE)
    assert stats.status == "complete"
    assert stats.crawl_frontier_exhausted
    _, journey = _journey(conn, entity)
    book_order = next(s for s in journey["stages"] if s["stage"] == "book_order")
    assert book_order["evidence_state"] == "not_observed_in_bounded_inspection"
    assert any("not the absence" in item for item in book_order["missing_knowledge"])
    conn.close()


# 6. ordering never manufactures pay
def test_ordering_never_manufactures_pay(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "no-pay.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=ORDERING_SITE)
    _, journey = _journey(conn, entity)
    assert _states(journey)["pay"] == "unknown"
    pay = next(s for s in journey["stages"] if s["stage"] == "pay")
    assert pay["evidence"] == [] and pay["channels"] == []
    conn.close()


# 7. booking/order never manufactures receive
def test_booking_never_manufactures_receive(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "no-receive.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=BOOKING_SITE)
    _, journey = _journey(conn, entity)
    assert _states(journey)["receive"] == "unknown"
    conn.close()


# 8. support page/channel reconstructs support
def test_support_page_reconstructs_support(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "support.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=SUPPORT_SITE)
    _, journey = _journey(conn, entity)
    support = next(s for s in journey["stages"] if s["stage"] == "support")
    assert support["evidence_state"] == "observed"
    assert any(c["channel_type"] == "support" for c in support["channels"])
    assert support["evidence"]
    conn.close()


# 9. customer-review text cannot create an operational journey stage
def test_review_text_never_creates_operational_stages(tmp_path: Path) -> None:
    without = prepared(tmp_path / "noreviews.sqlite")
    with_reviews = prepared(
        tmp_path / "reviews.sqlite", reviews=[REVIEW, dict(REVIEW, review_id="rev-j2")])
    entity = business_entity_id_for_maps_business(1)
    acquire(without, tmp_path, pages=MENU_ONLY_SITE)
    acquire(with_reviews, tmp_path, pages=MENU_ONLY_SITE)
    _, journey_a = _journey(without, entity)
    _, journey_b = _journey(with_reviews, entity)
    # Evidence IDS legitimately differ (the retained Maps snapshot embeds
    # the reviews, changing its content hash) — compare everything the
    # reviews could conceivably influence: states, channels, hand-offs,
    # missing knowledge, and the evidence SOURCE composition per stage.
    assert _states(journey_a) == _states(journey_b)
    for stage_a, stage_b in zip(journey_a["stages"], journey_b["stages"]):
        assert stage_a["stage"] == stage_b["stage"]
        assert stage_a["evidence_state"] == stage_b["evidence_state"]
        assert stage_a["missing_knowledge"] == stage_b["missing_knowledge"]
        assert [
            (c["channel_type"], c["normalized_identifier"])
            for c in stage_a["channels"]
        ] == [
            (c["channel_type"], c["normalized_identifier"])
            for c in stage_b["channels"]
        ]
        assert [
            (h["from"], h["to"]) for h in stage_a["handoffs"]
        ] == [
            (h["from"], h["to"]) for h in stage_b["handoffs"]
        ]
        assert sorted(e["source_id"] for e in stage_a["evidence"]) == sorted(
            e["source_id"] for e in stage_b["evidence"])
        if stage_b["stage"] in ("pay", "receive", "return"):
            assert stage_b["evidence_state"] == "unknown"
    assert [
        (h["from"], h["to"]) for h in journey_a["handoffs"]
    ] == [
        (h["from"], h["to"]) for h in journey_b["handoffs"]
    ]
    without.close()
    with_reviews.close()


# 10. stale supporting evidence cannot satisfy current Customer Journey
def test_stale_evidence_cannot_satisfy_journey(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "stale.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=BOOKING_SITE)
    far_future = "2027-01-15T12:00:00+00:00"
    _, journey = _journey(conn, entity, evaluated_at=far_future)
    states = _states(journey)
    assert all(state == "stale" for name, state in states.items()
               if name in ("discover", "evaluate", "contact", "book_order"))
    assert journey["current_stage_count"] == 0
    seal = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: far_future)
    state = conn.execute(
        "SELECT state FROM dossier_domain_assessments "
        "WHERE assessment_id=? AND domain='customer_journey'",
        (seal.assessment_id,)).fetchone()[0]
    assert state == "stale"
    conn.close()


# 11. conflicted transaction/capability facts propagate conflicted
def test_conflicted_facts_propagate_conflicted(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "conflicted.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=MENU_ONLY_SITE)
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    conflicted_fact = next(
        fact for fact in dossier["facts"]
        if fact["predicate"] == "capability.whatsapp")
    conflicted_fact = dict(conflicted_fact)
    conflicted_fact["status"] = "conflicted"
    patched_facts = [
        dict(fact) for fact in dossier["facts"]
    ]
    for index, fact in enumerate(patched_facts):
        if fact["predicate"] == "capability.whatsapp":
            patched_facts[index]["status"] = "conflicted"
    journey_doc, issues = reconstruct_customer_journey(
        conn, entity_id=entity, facts=patched_facts,
        evidence=dossier["evidence"], evaluated_at="2026-09-26T12:30:00+00:00")
    contact = next(s for s in journey_doc["stages"] if s["stage"] == "contact")
    assert contact["evidence_state"] == "conflicted"
    patched_dossier = dict(dossier)
    patched_dossier["customer_journey"] = journey_doc
    outcome = next(
        item for item in derive_domain_assessments(patched_dossier)
        if item["domain"] == "customer_journey")
    assert outcome["state"] == "conflicted"
    assert issues == []
    conn.close()


# 12. malformed journey provenance produces an integrity issue
def _forge_website_session(
    conn, *, entity, session_suffix, started_at, pages, bad_metadata=False
):
    """Insert a PRODUCER-VALID website session (config identity, counts)
    whose evidence rows may carry malformed metadata."""
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
            "INSERT INTO sources(id,source_type,name,base_url,created_at,active) "
            "VALUES ('src_official_web','official_website','Official website',NULL,"
            "'2026-01-01T00:00:00+00:00',1)")
    action_predicates = {
        "booking": "capability.online_booking",
        "ordering": "capability.online_ordering",
        "whatsapp": "capability.whatsapp",
    }
    config = wcanonical(
        {
            "entity_id": entity,
            "start_url": "https://forged.example/",
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
            "evidence_root": "/tmp/forged-ev",
        }
    )
    config_hash = wsha(config)
    session_id = wopaque("acq", entity, started_at, config_hash)
    total_observations = sum(
        1
        for candidate_list in [c for _, _, c in pages]
        for candidate in candidate_list
        if candidate["channel_type"] in action_predicates
    )
    # YJ-01/YJ-03: the real producer lifecycle — running, children,
    # then ONE finalization UPDATE carrying status, lifecycle, counters,
    # and the child seal together. Born-terminal website sessions are
    # rejected by migration v5.
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,'running',?,NULL,NULL,NULL,0,0)",
        (session_id, entity, "src_official_web", "sara.website",
         COLLECTOR_VERSION, config, config_hash, started_at))
    for index, (final_url, content, channels) in enumerate(pages):
        content_sha = wsha(content)
        evidence_id = wopaque("ev", session_id, final_url, content_sha)
        # Producer-faithful: the parser detects an action channel iff the
        # page carries it, and the producer then emits the matching
        # capability observation bound to this evidence row.
        observation_ids = []
        pending_observations = []
        for candidate in channels:
            predicate = action_predicates.get(candidate["channel_type"])
            if predicate is None:
                continue
            value_json = wcanonical(True)
            observation_id = wopaque(
                "obs", evidence_id, predicate, wsha(value_json))
            pending_observations.append(
                (observation_id, entity, predicate, evidence_id,
                 value_json, value_json, wsha(value_json),
                 "detected_capability", started_at, started_at,
                 "heuristic", "sara.website", "5", 0.8, started_at))
            observation_ids.append(observation_id)
        metadata = (
            "not-json-at-all"
            if bad_metadata and index == 0
            else wcanonical(
                {
                    "acquisition_kind": "bounded_official_website",
                    "entity_id": entity,
                    "start_url": "https://forged.example/",
                    "requested_url": final_url,
                    "final_url": final_url,
                    "crawl_depth": 0,
                    "page_role": "home",
                    "home_page": index == 0,
                    "business_wide_scope_eligible": True,
                    "crawl_frontier_exhausted": True,
                    "title": None,
                    "canonical_url": final_url,
                    "channels": channels,
                    "observation_ids": sorted(observation_ids),
                }
            )
        )
        conn.execute(
            "INSERT INTO evidence_items("
            "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
            "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
            ") VALUES (?,?,?,?,?,'usable',?,NULL,NULL,'text/html',?,NULL,?,?)",
            (evidence_id, session_id, "src_official_web", final_url,
             "official", started_at, content_sha, metadata, started_at))
        for row in pending_observations:
            conn.execute(
                "INSERT INTO observations("
                "id,subject_id,predicate,evidence_id,value_json,"
                "normalized_value_json,value_hash,observation_kind,"
                "observed_at,extracted_at,extraction_method,extractor_name,"
                "extractor_version,confidence,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row)
    from sara.storage import session_child_seal_digest

    conn.execute(
        "UPDATE acquisition_sessions SET status='complete', finished_at=?, "
        "error=NULL, evidence_count=?, observation_count=?, child_seal_sha256=? "
        "WHERE id=? AND status='running'",
        (started_at, len(pages), total_observations,
         session_child_seal_digest(conn, session_id), session_id))
    conn.commit()
    return session_id


def test_malformed_website_metadata_produces_integrity_issue(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "malformed.sqlite")
    entity = business_entity_id_for_maps_business(1)
    _forge_website_session(
        conn, entity=entity, session_suffix="badmeta",
        started_at="2026-09-26T12:10:00+00:00",
        pages=[("https://forged.example/", "page-one", [])],
        bad_metadata=True,
    )
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    codes = {i["code"] for i in dossier["integrity_issues"]}
    assert "customer_journey_website_evidence_invalid" in codes
    issue = next(
        i for i in dossier["integrity_issues"]
        if i["code"] == "customer_journey_website_evidence_invalid")
    assert issue["evidence_id"].startswith("ev_")
    used = {
        entry["evidence_id"]
        for stage in dossier["customer_journey"]["stages"]
        for entry in stage["evidence"]
    }
    assert issue["evidence_id"] not in used
    conn.close()


# 13. same evidence + same evaluation time is byte-identical
def test_reconstruction_is_deterministic(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "deterministic.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=BOOKING_SITE)
    first = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    second = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    assert json.dumps(first["customer_journey"], sort_keys=True) == json.dumps(
        second["customer_journey"], sort_keys=True)
    conn.close()


# 14. material journey evidence change invalidates the persisted assessment
def test_material_change_invalidates_fingerprint(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "invalidate.sqlite")
    entity = business_entity_id_for_maps_business(1)
    clock = Clock()
    acquire(conn, tmp_path, pages=MENU_ONLY_SITE, clock=clock)
    before = understanding_state_fingerprint(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    # A second crawl adds a booking action link: material journey evidence.
    acquire(conn, tmp_path, pages=BOOKING_SITE, clock=clock)
    after = understanding_state_fingerprint(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    assert before["input_signature_sha256"] != after["input_signature_sha256"]
    conn.close()


# 15. sara-dossier remains read-only and exposes the journey
def test_dossier_cli_read_only_exposes_journey(tmp_path: Path, capsys) -> None:
    from sara.dossier.surface import main as dossier_main

    db = tmp_path / "cli.sqlite"
    conn = prepared(db)
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=BOOKING_SITE)
    conn.close()
    rc = dossier_main(["--db", str(db), "--entity-id", entity])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    journey = payload["customer_journey"]
    assert journey["reconstruction_version"] == RECONSTRUCTION_VERSION
    assert journey["current_stage_count"] >= 3


# 16. no evaluation vocabulary anywhere in the journey document
def test_no_gap_or_quality_vocabulary(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "vocab.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=BOOKING_SITE)
    _, journey = _journey(conn, entity)

    allowed_stage_keys = {
        "stage", "stage_label", "evidence_state", "channels", "handoffs",
        "evidence", "missing_knowledge",
    }
    allowed_channel_keys = {
        "channel_type", "identifier", "normalized_identifier", "scope",
        "evidence_id",
    }
    allowed_evidence_keys = {
        "evidence_id", "source_id", "source_locator", "content_sha256",
        "retrieved_at", "acquisition_session_id", "collector_name",
        "collector_version", "acquisition_status", "observation_ids",
    }
    allowed_doc_keys = {
        "reconstruction_version", "stages", "handoffs",
        "observed_stage_count", "current_stage_count",
        "public_surface_coverage",
    }
    assert set(journey) == allowed_doc_keys
    assert set(journey["public_surface_coverage"]) == {
        "state", "evidence_ids", "session_ids", "support",
    }
    assert journey["public_surface_coverage"]["state"] in {
        "evaluation_observed",
        "bounded_inspection_no_evaluation",
        "not_covered",
    }
    for item in journey["public_surface_coverage"]["support"]:
        assert set(item) == {
            "evidence_id", "acquisition_session_id", "retrieved_at",
        }
    for stage in journey["stages"]:
        assert set(stage) == allowed_stage_keys
        assert stage["evidence_state"] in {
            "observed", "not_observed_in_bounded_inspection", "unknown",
            "stale", "conflicted",
        }
        for channel in stage["channels"]:
            assert set(channel) == allowed_channel_keys
        for entry in stage["evidence"]:
            assert set(entry) == allowed_evidence_keys
    forbidden = (
        "score", "friction", "opportunity", "recommendation", "quality",
        "good", "bad", "better", "worse", "gap", "strength", "weakness",
    )
    blob = json.dumps(journey).lower()
    for word in forbidden:
        assert word not in blob, word
    conn.close()



# ---- CJ acceptance regressions (frozen review 5372679339) ----

def test_two_entity_isolation(tmp_path: Path) -> None:
    """CJ-01: another canonical Entity's website evidence is neither used
    nor an integrity failure while reading this Entity."""
    conn = prepared(tmp_path / "isolation.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=MENU_ONLY_SITE)
    before = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    # A second, unrelated canonical Entity with its own producer-valid
    # website session (booking link, malformed-free).
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,created_at,updated_at) "
        "VALUES ('be_other','business_entity','active',"
        "'2026-09-25T00:00:00+00:00','2026-09-26T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO business_entities(id,display_name,entity_type,lifecycle_status,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("be_other", "Other", "independent_business", "operating",
         "2026-09-25T00:00:00+00:00", "2026-09-26T00:00:00+00:00"))
    booking_channel = [{
        "channel_type": "booking",
        "identifier": "https://booksy.com/other",
        "normalized_identifier": "https://booksy.com/other",
        "url": "https://booksy.com/other",
        "extraction": "action_link",
        "canonicalized_channel_id": None,
        "canonicalized_scope": None,
    }]
    _forge_website_session(
        conn, entity="be_other", session_suffix="other",
        started_at="2026-09-26T12:20:00+00:00",
        pages=[("https://other.example/", "other-home", booking_channel)])
    after = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    assert after["integrity_issues"] == before["integrity_issues"]
    assert after["customer_journey"] == before["customer_journey"]
    used = {
        entry["evidence_id"]
        for stage in after["customer_journey"]["stages"]
        for entry in stage["evidence"]
    }
    other_rows = {
        row[0] for row in conn.execute(
            "SELECT e.id FROM evidence_items e "
            "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
            "WHERE a.target_subject_id='be_other'").fetchall()
    }
    assert not (used & other_rows)
    conn.close()


def test_predecessor_entity_convergence_admits_evidence(tmp_path: Path) -> None:
    """CJ-01: evidence acquired under a predecessor Entity before the
    merge remains the same business's history after convergence."""
    conn = prepared(tmp_path / "convergence.sqlite")
    entity = business_entity_id_for_maps_business(1)
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,merged_into_subject_id,"
        "merged_at,created_at,updated_at) "
        "VALUES ('be_old','business_entity','merged',?,"
        "'2026-09-26T11:00:00+00:00','2026-09-25T00:00:00+00:00','2026-09-26T11:00:00+00:00')",
        (entity,))
    booking_channel = [{
        "channel_type": "booking",
        "identifier": "https://booksy.com/legacy",
        "normalized_identifier": "https://booksy.com/legacy",
        "url": "https://booksy.com/legacy",
        "extraction": "action_link",
        "canonicalized_channel_id": None,
        "canonicalized_scope": None,
    }]
    _forge_website_session(
        conn, entity="be_old", session_suffix="legacy",
        started_at="2026-09-26T10:30:00+00:00",
        pages=[("https://legacy.example/", "legacy-home", booking_channel)])
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    codes = {i["code"] for i in dossier["integrity_issues"]}
    assert "customer_journey_website_session_invalid" not in codes
    assert "customer_journey_website_evidence_invalid" not in codes
    book_order = next(
        s for s in dossier["customer_journey"]["stages"]
        if s["stage"] == "book_order")
    assert book_order["evidence_state"] == "observed"
    assert any(c["channel_type"] == "booking" for c in book_order["channels"])
    conn.close()


def test_forged_child_under_terminal_session_invalidates_session(tmp_path: Path) -> None:
    """XJ-01 (decisive): a perfectly-shaped evidence row appended beneath
    a finalized website session is rejected BY THE DATABASE — the child
    seal makes post-finalization membership unenforceable-by-forge, so
    the journey never even sees it."""
    import sqlite3

    conn = prepared(tmp_path / "forged.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = acquire(conn, tmp_path, pages=BOOKING_SITE)
    session_id = stats.session_id
    sealed = conn.execute(
        "SELECT child_seal_sha256 FROM acquisition_sessions WHERE id=?",
        (session_id,)).fetchone()[0]
    assert sealed
    forged_url = "https://seed.example/forged"
    from sara.website.model import canonical_json as wcanonical
    from sara.website.model import opaque_id as wopaque, sha256_text as wsha

    forged_sha = wsha("forged page body")
    forged_id = wopaque("ev", session_id, forged_url, forged_sha)
    forged_meta = wcanonical(
        {
            "acquisition_kind": "bounded_official_website",
            "entity_id": entity,
            "start_url": "https://seed.example/",
            "requested_url": forged_url,
            "final_url": forged_url,
            "crawl_depth": 0,
            "page_role": "booking",
            "home_page": False,
            "business_wide_scope_eligible": True,
            "crawl_frontier_exhausted": True,
            "title": None,
            "canonical_url": forged_url,
            "channels": [{
                "channel_type": "booking",
                "identifier": "https://booksy.com/forged",
                "normalized_identifier": "https://booksy.com/forged",
                "url": "https://booksy.com/forged",
                "extraction": "action_link",
                "canonicalized_channel_id": None,
                "canonicalized_scope": None,
            }],
            "observation_ids": [],
        }
    )
    with pytest.raises(sqlite3.IntegrityError, match="sealed acquisition session"):
        conn.execute(
            "INSERT INTO evidence_items("
            "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
            "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
            ") VALUES (?,?,?,'https://seed.example/forged','official','usable',"
            "?,NULL,NULL,'text/html',?,NULL,?,?)",
            (forged_id, session_id, "src_official_web",
             "2026-09-26T12:00:09+00:00", forged_sha, forged_meta,
             "2026-09-26T12:00:09+00:00"))
    # Counter correction is equally rejected.
    stored = conn.execute(
        "SELECT evidence_count FROM acquisition_sessions WHERE id=?",
        (session_id,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="counters are immutable"):
        conn.execute(
            "UPDATE acquisition_sessions SET evidence_count=? WHERE id=?",
            (stored + 1, session_id))
    # The seal itself cannot be rewritten.
    with pytest.raises(sqlite3.IntegrityError, match="seal is immutable"):
        conn.execute(
            "UPDATE acquisition_sessions SET child_seal_sha256=? WHERE id=?",
            ("0" * 64, session_id))
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    channels = {
        (c["identifier"], c["normalized_identifier"])
        for stage in dossier["customer_journey"]["stages"]
        for c in stage["channels"]
    }
    assert ("https://booksy.com/forged", "https://booksy.com/forged") not in (
        channels
    )
    conn.close()

def test_maps_only_stale_journey_is_stale_not_vanished(tmp_path: Path) -> None:
    """CJ-03: Maps-only evidence that ages out marks its stages stale —
    previously observed stages never collapse to unknown/not_started."""
    conn = prepared(tmp_path / "maps-stale.sqlite")
    entity = business_entity_id_for_maps_business(1)
    far_future = "2027-01-15T12:00:00+00:00"
    _, journey = _journey(conn, entity, evaluated_at=far_future)
    states = _states(journey)
    assert states["discover"] == "stale"
    assert states["contact"] == "stale"
    assert journey["observed_stage_count"] >= 2
    assert journey["current_stage_count"] == 0
    seal = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: far_future)
    state = conn.execute(
        "SELECT state FROM dossier_domain_assessments "
        "WHERE assessment_id=? AND domain='customer_journey'",
        (seal.assessment_id,)).fetchone()[0]
    assert state == "stale"
    conn.close()


def test_mixed_freshness_observed_stage_shows_current_surface_only(tmp_path: Path) -> None:
    """RCJ-04: with a fresh and a stale crawl of the same site, an observed
    stage exposes only current surface — WITHOUT re-aging fact evidence
    against a second universal window. Website metadata channels cite the
    current crawl; a current phone fact keeps its (older) Maps evidence;
    no stale-crawl WEBSITE rows appear in observed stages."""
    conn = prepared(tmp_path / "mixed.sqlite")
    entity = business_entity_id_for_maps_business(1)
    clock = Clock()
    acquire(conn, tmp_path, pages=BOOKING_SITE, clock=clock)
    first_rows = {
        row[0] for row in conn.execute(
            "SELECT e.id FROM evidence_items e "
            "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
            "WHERE a.collector_name='sara.website'").fetchall()
    }
    clock.current += timedelta(days=40)
    acquire(conn, tmp_path, pages=BOOKING_SITE, clock=clock)
    all_rows = {
        row[0] for row in conn.execute(
            "SELECT e.id FROM evidence_items e "
            "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
            "WHERE a.collector_name='sara.website'").fetchall()
    }
    stale_website_rows = first_rows
    fresh_website_rows = all_rows - first_rows
    assert fresh_website_rows
    evaluated = "2026-11-10T12:00:00+00:00"  # 5 days after the second crawl
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at=evaluated)
    journey = dossier["customer_journey"]
    observed = [
        s for s in journey["stages"] if s["evidence_state"] == "observed"
    ]
    assert observed
    maps_rows = {
        row[0] for row in conn.execute(
            "SELECT e.id FROM evidence_items e WHERE e.source_id='src_google_maps'"
        ).fetchall()
    }
    current_fact_evidence = {
        str(support["evidence_id"])
        for fact in dossier["facts"]
        if fact["status"] in {"confirmed", "single_source"}
        and not bool(fact["freshness"]["is_stale"])
        for support in fact["observation_support"]
        if support.get("support_role") == "supports"
        and support.get("evidence_status") == "usable"
    }
    for stage in observed:
        # Stale-crawl website rows may appear ONLY as supporting
        # observations of CURRENT facts (RCJ-04: fact currency follows
        # the fact, its confirming observations are not re-aged). They
        # must never arrive through the metadata path — channels and
        # hand-offs below pin that.
        used = {entry["evidence_id"] for entry in stage["evidence"]}
        for evidence_id in used & stale_website_rows:
            assert evidence_id in current_fact_evidence, (
                stage["stage"], evidence_id,
                "stale website row outside current-fact support")
        # Current fact evidence (e.g. the 60-day phone fact citing older
        # Maps rows) legitimately remains — it was never re-aged.
        if stage["stage"] == "contact":
            assert used & maps_rows, "phone fact Maps evidence retained"
        # Website metadata channels cite only current-crawl rows...
        for channel in stage["channels"]:
            if channel["evidence_id"] in all_rows:
                assert channel["evidence_id"] in fresh_website_rows
        # ...and current hand-offs likewise.
        for handoff in stage["handoffs"]:
            if handoff["from"] == "official_website":
                assert handoff["evidence_id"] in fresh_website_rows
                assert handoff["current"] is True
    for handoff in journey["handoffs"]:
        if handoff["from"] == "official_website":
            assert handoff["evidence_id"] in fresh_website_rows
    # The maps->website hand-off cites Maps evidence whose currency
    # follows the official-website fact's 90-day window — present here.
    assert any(
        handoff["from"] == "maps_listing"
        for handoff in journey["handoffs"]
    )
    assert stale_website_rows
    conn.close()
def test_preview_consistent_with_reconstruction(tmp_path: Path) -> None:
    """CJ-05: the read-only preview reads the reconstruction — never
    contradicting it and never claiming sufficiency."""
    from sara.dossier.status import preview_domains

    conn = prepared(tmp_path / "preview.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=SUPPORT_SITE)
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    preview = {
        item["domain"]: item["state"]
        for item in dossier["dossier_status"]["read_only_preview"]["domains"]
    }
    # The real assessment is sufficient; the preview must agree that
    # stages exist WITHOUT promoting to sufficient.
    assert preview["customer_journey"] == "partial"
    seal = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:30:00+00:00")
    assert "customer_journey" not in seal.blocking_mandatory_domains

    # Unit contract for the mapping itself: sufficient reconstruction
    # previews partial; stale previews stale; empty previews not_started.
    from datetime import datetime as _dt, timezone as _tz
    evaluation = _dt(2026, 9, 26, tzinfo=_tz.utc)

    def _preview_for(journey):
        return {
            item["domain"]: item["state"]
            for item in preview_domains(
                [], [], [], [], evaluation, customer_journey=journey)
        }

    observed_stage = {
        "stage": "discover", "evidence_state": "observed",
        "channels": [], "handoffs": [], "evidence": [],
        "missing_knowledge": [],
    }
    stale_stage = dict(observed_stage, evidence_state="stale")
    conflicted_stage = dict(observed_stage, evidence_state="conflicted")
    assert _preview_for(
        {"stages": [observed_stage]})["customer_journey"] == "partial"
    assert _preview_for(
        {"stages": [stale_stage]})["customer_journey"] == "stale"
    assert _preview_for(
        {"stages": [conflicted_stage]})["customer_journey"] == "conflicted"
    assert _preview_for(
        {"stages": []})["customer_journey"] == "not_started"
    assert _preview_for(None)["customer_journey"] == "not_started"
    conn.close()



# ---- RCJ acceptance regressions (frozen review 5375391470) ----

def test_unsupported_website_version_is_silently_out_of_scope(tmp_path: Path) -> None:
    """RCJ-01: a producer-valid session at a version the journey simply
    does not know is NOT an integrity failure — durable history must not
    be poisoned by collector version evolution."""
    conn = prepared(tmp_path / "v4.sqlite")
    entity = business_entity_id_for_maps_business(1)
    from sara.website.model import (
        canonical_json as wcanonical,
        opaque_id as wopaque,
        sha256_text as wsha,
    )

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
    started_at = "2026-09-26T12:15:00+00:00"
    session_id = wopaque("acq", entity, started_at, wsha(config))
    conn.execute(
        "INSERT INTO sources(id,source_type,name,base_url,created_at,active) "
        "VALUES ('src_official_web','official_website','Official website',NULL,"
        "'2026-01-01T00:00:00+00:00',1)")
    # The full real lifecycle at version 4: the session is structurally
    # perfect — exclusion is attributable to VERSION alone.
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,'running',?,NULL,NULL,NULL,0,0)",
        (session_id, entity, "src_official_web", "sara.website", "4",
         config, wsha(config), started_at))
    conn.commit()
    # R4: the v4 session carries a UNIQUE booking endpoint; exclusion is
    # asserted through acquisition_session_id (the reviewer-corrected
    # contract — evidence ids can never equal session ids) and through
    # the endpoint never surfacing in any channel.
    from sara.website.model import (
        canonical_json as wcanonical,
        opaque_id as wopaque,
        sha256_text as wsha,
    )

    page_url = "https://legacy.example/booking"
    content = "legacy booking page"
    content_sha = wsha(content)
    evidence_id = wopaque("ev", session_id, page_url, content_sha)
    booking_channel = [{
        "channel_type": "booking",
        "identifier": "https://booksy.com/legacy-v4",
        "normalized_identifier": "https://booksy.com/legacy-v4",
        "url": "https://booksy.com/legacy-v4",
        "extraction": "action_link",
        "canonicalized_channel_id": None,
        "canonicalized_scope": None,
    }]
    observation_json = wcanonical(True)
    observation_id = wopaque(
        "obs", evidence_id, "capability.online_booking",
        wsha(observation_json))
    metadata = wcanonical(
        {
            "acquisition_kind": "bounded_official_website",
            "entity_id": entity,
            "start_url": "https://legacy.example/",
            "requested_url": page_url,
            "final_url": page_url,
            "crawl_depth": 0,
            "page_role": "booking",
            "home_page": False,
            "business_wide_scope_eligible": True,
            "crawl_frontier_exhausted": True,
            "title": None,
            "canonical_url": page_url,
            "channels": booking_channel,
            "observation_ids": [observation_id],
        }
    )
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,?,'usable',?,NULL,NULL,'text/html',?,NULL,?,?)",
        (evidence_id, session_id, "src_official_web", page_url, "official",
         started_at, content_sha, metadata, started_at))
    conn.execute(
        "INSERT INTO observations("
        "id,subject_id,predicate,evidence_id,value_json,"
        "normalized_value_json,value_hash,observation_kind,"
        "observed_at,extracted_at,extraction_method,extractor_name,"
        "extractor_version,confidence,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (observation_id, entity, "capability.online_booking", evidence_id,
         observation_json, observation_json, wsha(observation_json),
         "detected_capability", started_at, started_at, "heuristic",
         "sara.website", "4", 0.8, started_at))
    from sara.storage import session_child_seal_digest

    conn.execute(
        "UPDATE acquisition_sessions SET status='complete', finished_at=?, "
        "error=NULL, evidence_count=1, observation_count=1, "
        "child_seal_sha256=? WHERE id=? AND status='running'",
        (started_at, session_child_seal_digest(conn, session_id), session_id))
    conn.commit()
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    codes = {i["code"] for i in dossier["integrity_issues"]}
    assert "customer_journey_website_session_invalid" not in codes
    assert "customer_journey_website_evidence_invalid" not in codes
    # Out of scope means not used, not flagged — asserted through the
    # session id, which CAN legitimately appear in evidence entries.
    used_sessions = {
        entry["acquisition_session_id"]
        for stage in dossier["customer_journey"]["stages"]
        for entry in stage["evidence"]
    }
    assert session_id not in used_sessions
    all_channels = {
        (c["identifier"], c["normalized_identifier"])
        for stage in dossier["customer_journey"]["stages"]
        for c in stage["channels"]
    }
    assert ("https://booksy.com/legacy-v4", "https://booksy.com/legacy-v4") not in (
        all_channels
    )
    assert not any(
        h["evidence_id"] == evidence_id
        for h in dossier["customer_journey"]["handoffs"]
    )
    conn.close()


def test_future_version_with_extra_config_keys_stays_admissible(tmp_path: Path) -> None:
    """RCJ-01: a session at a FUTURE version whose config adds keys beyond
    the required contract remains admissible — version bumps must not
    poison durable history."""
    conn = prepared(tmp_path / "v6.sqlite")
    entity = business_entity_id_for_maps_business(1)
    from sara.website.model import (
        canonical_json as wcanonical,
        opaque_id as wopaque,
        sha256_text as wsha,
    )

    config_dict = {
        "entity_id": entity,
        "start_url": "https://seed.example/",
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
        "evidence_root": "/tmp/future-ev",
        "screenshot": True,  # a hypothetical v6 addition
    }
    config = wcanonical(config_dict)
    started_at = "2026-09-26T12:15:00+00:00"
    session_id = wopaque("acq", entity, started_at, wsha(config))
    conn.execute(
        "INSERT INTO sources(id,source_type,name,base_url,created_at,active) "
        "VALUES ('src_official_web','official_website','Official website',NULL,"
        "'2026-01-01T00:00:00+00:00',1)")
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,'running',?,NULL,NULL,NULL,0,0)",
        (session_id, entity, "src_official_web", "sara.website", "6",
         config, wsha(config), started_at))
    booking_channel = [{
        "channel_type": "booking",
        "identifier": "https://booksy.com/future",
        "normalized_identifier": "https://booksy.com/future",
        "url": "https://booksy.com/future",
        "extraction": "action_link",
        "canonicalized_channel_id": None,
        "canonicalized_scope": None,
    }]
    page_url = "https://seed.example/booking"
    content = "future booking page"
    content_sha = wsha(content)
    evidence_id = wopaque("ev", session_id, page_url, content_sha)
    metadata = wcanonical(
        {
            "acquisition_kind": "bounded_official_website",
            "entity_id": entity,
            "start_url": "https://seed.example/",
            "requested_url": page_url,
            "final_url": page_url,
            "crawl_depth": 0,
            "page_role": "booking",
            "home_page": False,
            "business_wide_scope_eligible": True,
            "crawl_frontier_exhausted": True,
            "title": None,
            "canonical_url": page_url,
            "channels": booking_channel,
            "observation_ids": [],
            "screenshot_ref": "x",  # hypothetical v6 metadata addition
        }
    )
    # Producer-faithful: the booking channel implies its capability
    # observation on this row (R1 seal).
    value_json = wcanonical(True)
    observation_id = wopaque(
        "obs", evidence_id, "capability.online_booking", wsha(value_json))
    metadata = wcanonical(
        json.loads(metadata) | {"observation_ids": [observation_id]})
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,?,'usable',?,NULL,NULL,'text/html',?,NULL,?,?)",
        (evidence_id, session_id, "src_official_web", page_url, "official",
         started_at, content_sha, metadata, started_at))
    conn.execute(
        "INSERT INTO observations("
        "id,subject_id,predicate,evidence_id,value_json,"
        "normalized_value_json,value_hash,observation_kind,"
        "observed_at,extracted_at,extraction_method,extractor_name,"
        "extractor_version,confidence,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (observation_id, entity, "capability.online_booking", evidence_id,
         value_json, value_json, wsha(value_json), "detected_capability",
         started_at, started_at, "heuristic", "sara.website", "6",
         0.8, started_at))
    from sara.storage import session_child_seal_digest

    conn.execute(
        "UPDATE acquisition_sessions SET status='complete', finished_at=?, "
        "error=NULL, evidence_count=1, observation_count=1, "
        "child_seal_sha256=? WHERE id=? AND status='running'",
        (started_at, session_child_seal_digest(conn, session_id), session_id))
    conn.commit()
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    codes = {i["code"] for i in dossier["integrity_issues"]}
    assert "customer_journey_website_session_invalid" not in codes
    assert "customer_journey_website_evidence_invalid" not in codes
    book_order = next(
        s for s in dossier["customer_journey"]["stages"]
        if s["stage"] == "book_order")
    assert book_order["evidence_state"] == "observed"
    assert any(
        c["normalized_identifier"] == "https://booksy.com/future"
        for c in book_order["channels"]
    )
    conn.close()


def test_absence_from_old_version_or_foreign_source_not_bounded(tmp_path: Path) -> None:
    """RCJ-01: bounded not-observed requires absence support from the
    WEBSITE collector at an absence-safe version (>= v5, when frontier
    semantics became trustworthy). Pre-v5 or non-website absence support
    leaves the stage unknown."""
    from sara.dossier.customer_journey import reconstruct_customer_journey

    conn = prepared(tmp_path / "absence.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=MENU_ONLY_SITE)  # v5 absence facts exist
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    # Unit-level: same fact, absence support downgraded to v4 or a
    # foreign source/collector, must NOT establish bounded not-observed.
    base_fact = next(
        fact for fact in dossier["facts"]
        if fact["predicate"] == "capability.online_booking"
        and fact["status"] == "not_observed")

    def _variant(source_id, collector_name, version):
        fact = json.loads(json.dumps(base_fact))
        for support in fact["acquisition_support"]:
            support["source_id"] = source_id
            support["collector_name"] = collector_name
            support["collector_version"] = version
        return fact

    facts_v4 = [dict(f) for f in dossier["facts"]]
    for index, fact in enumerate(facts_v4):
        if fact["id"] == base_fact["id"]:
            facts_v4[index] = _variant(
                "src_official_web", "sara.website", "4")
    doc_v4, _ = reconstruct_customer_journey(
        conn, entity_id=entity, facts=facts_v4, evidence=dossier["evidence"],
        evaluated_at="2026-09-26T12:30:00+00:00")
    stage_v4 = next(
        s for s in doc_v4["stages"] if s["stage"] == "book_order")
    assert stage_v4["evidence_state"] == "unknown"

    facts_foreign = [dict(f) for f in dossier["facts"]]
    for index, fact in enumerate(facts_foreign):
        if fact["id"] == base_fact["id"]:
            facts_foreign[index] = _variant(
                "src_google_maps", "sara.maps_sync", "2")
    doc_foreign, _ = reconstruct_customer_journey(
        conn, entity_id=entity, facts=facts_foreign,
        evidence=dossier["evidence"],
        evaluated_at="2026-09-26T12:30:00+00:00")
    stage_foreign = next(
        s for s in doc_foreign["stages"] if s["stage"] == "book_order")
    assert stage_foreign["evidence_state"] == "unknown"
    conn.close()


def test_source_time_identity_forgeries_are_invalid(tmp_path: Path) -> None:
    """RCJ-02: source_id, legacy_run_id, and target==config-entity are
    part of the producer contract. Forging any of them invalidates the
    session even when config bytes, hash, and id are self-consistent."""
    conn = prepared(tmp_path / "identity.sqlite")
    entity = business_entity_id_for_maps_business(1)
    # Predecessor entity whose sessions are lineage-admissible.
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,record_state,merged_into_subject_id,"
        "merged_at,created_at,updated_at) "
        "VALUES ('be_old','business_entity','merged',?,"
        "'2026-09-26T11:00:00+00:00','2026-09-25T00:00:00+00:00','2026-09-26T11:00:00+00:00')",
        (entity,))
    from sara.website.model import (
        canonical_json as wcanonical,
        opaque_id as wopaque,
        sha256_text as wsha,
    )

    def _session(target, config_entity, *, legacy_run_id=None,
                 source_id="src_official_web", suffix=""):
        config = wcanonical(
            {
                "entity_id": config_entity,
                "start_url": "https://seed.example/",
                "page_limit": 8, "depth_limit": 2,
                "max_response_bytes": 1048576, "timeout_seconds": 10.0,
                "request_interval_seconds": 1.0,
                "max_policy_delay_seconds": 30.0,
                "retry_attempt_limit": 4,
                "retry_base_delay_seconds": 1.0,
                "retry_max_delay_seconds": 30.0,
                "retry_delay_budget_seconds": 60.0,
                "user_agent": "SaraBusinessUnderstanding/1.0",
                "obey_robots": True,
                "evidence_root": "/tmp/x",
            }
        )
        started_at = f"2026-09-26T12:1{suffix}:00+00:00"
        session_id = wopaque(
            "acq", config_entity, started_at, wsha(config))
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,"
            "config_json,config_hash,status,started_at,finished_at,error,"
            "legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,'running',?,NULL,NULL,?,0,0)",
            (session_id, target, source_id, "sara.website", "5",
             config, wsha(config), started_at, legacy_run_id))
        # Finalize without children (blocked/complete): the empty-set
        # seal keeps the lifecycle honest for complete.
        from sara.storage import session_child_seal_digest

        conn.execute(
            "UPDATE acquisition_sessions SET status='complete', finished_at=?, "
            "error=NULL, evidence_count=0, observation_count=0, "
            "child_seal_sha256=? WHERE id=? AND status='running'",
            (started_at, session_child_seal_digest(conn, session_id),
             session_id))
        return session_id

    if conn.execute(
        "SELECT 1 FROM sources WHERE id='src_official_web'"
    ).fetchone() is None:
        conn.execute(
            "INSERT INTO sources(id,source_type,name,base_url,created_at,active) "
            "VALUES ('src_official_web','official_website','Official website',NULL,"
            "'2026-01-01T00:00:00+00:00',1)")
    # A dedicated run row so legacy_run_id is FK-valid yet unused by any
    # real session (the insert collision guard rejects duplicates).
    conn.execute(
        "INSERT INTO runs(id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,"
        "config_json,raw_path,status,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("rx", "test", '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
         2.0, 1, '["restaurant"]', "img", "{}", "/e/rx.jsonl", "complete",
         "2026-09-26T12:00:00+00:00"))
    # target=canonical, config=predecessor: both in lineage, but the
    # source-time identity disagrees — invalid (RCJ-02).
    sid_mismatch = _session(entity, "be_old", suffix="1")
    # producer contract violations:
    sid_legacy = _session(entity, entity, legacy_run_id="rx", suffix="2")
    sid_source = _session(
        entity, entity, source_id="src_google_maps", suffix="3")
    conn.commit()
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    invalid_sessions = {
        issue["session_id"] for issue in dossier["integrity_issues"]
        if issue["code"] == "customer_journey_website_session_invalid"
    }
    assert sid_mismatch in invalid_sessions
    assert sid_legacy in invalid_sessions
    assert sid_source in invalid_sessions
    conn.close()


def test_terminal_lifecycle_decisive_set(tmp_path: Path) -> None:
    """YJ-01 decisive regressions: the seal is bound to the terminal
    transition. Born-terminal website sessions are rejected at insert;
    a terminal transition without the seal fails; the legitimate
    running->children->complete+counters+seal lifecycle succeeds; a
    pre-v5-style terminal-unsealed session can never be sealed after
    the fact; post-seal mutation stays blocked; and a producer-side
    wrong-digest lie is caught by the reader recompute."""
    import sqlite3

    from sara.storage import session_child_seal_digest
    from sara.website.model import canonical_json as wcanonical
    from sara.website.model import opaque_id as wopaque, sha256_text as wsha

    conn = prepared(tmp_path / "lifecycle.sqlite")
    entity = business_entity_id_for_maps_business(1)
    if conn.execute(
        "SELECT 1 FROM sources WHERE id='src_official_web'"
    ).fetchone() is None:
        conn.execute(
            "INSERT INTO sources(id,source_type,name,base_url,created_at,active) "
            "VALUES ('src_official_web','official_website','Official website',NULL,"
            "'2026-01-01T00:00:00+00:00',1)")

    def _config(start_url="https://seed.example/"):
        return wcanonical(
            {
                "entity_id": entity, "start_url": start_url,
                "page_limit": 8, "depth_limit": 2,
                "max_response_bytes": 1048576, "timeout_seconds": 10.0,
                "request_interval_seconds": 1.0,
                "max_policy_delay_seconds": 30.0,
                "retry_attempt_limit": 4, "retry_base_delay_seconds": 1.0,
                "retry_max_delay_seconds": 30.0,
                "retry_delay_budget_seconds": 60.0,
                "user_agent": "SaraBusinessUnderstanding/1.0",
                "obey_robots": True, "evidence_root": "/tmp/ev",
            }
        )

    def _insert_running(session_id, started_at, config):
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,"
            "config_json,config_hash,status,started_at,finished_at,error,"
            "legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,'running',?,NULL,NULL,NULL,0,0)",
            (session_id, entity, "src_official_web", "sara.website", "5",
             config, wsha(config), started_at))

    # (1) Direct terminal insertion is rejected outright.
    config_a = _config()
    started_a = "2026-09-26T12:01:00+00:00"
    sid_a = wopaque("acq", entity, started_a, wsha(config_a))
    with pytest.raises(
        sqlite3.IntegrityError,
        match="finalized from running",
    ):
        conn.execute(
            "INSERT INTO acquisition_sessions("
            "id,target_subject_id,source_id,collector_name,collector_version,"
            "config_json,config_hash,status,started_at,finished_at,error,"
            "legacy_run_id,evidence_count,observation_count"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid_a, entity, "src_official_web", "sara.website", "5",
             config_a, wsha(config_a), "complete", started_a, started_a,
             None, None, 0, 0))

    # (2) running -> complete WITHOUT the seal fails.
    config_b = _config("https://seed.example/b/")
    started_b = "2026-09-26T12:02:00+00:00"
    sid_b = wopaque("acq", entity, started_b, wsha(config_b))
    _insert_running(sid_b, started_b, config_b)
    with pytest.raises(
        sqlite3.IntegrityError, match="requires its child seal"
    ):
        conn.execute(
            "UPDATE acquisition_sessions SET status='complete', finished_at=?, "
            "error=NULL, evidence_count=0, observation_count=0 "
            "WHERE id=? AND status='running'",
            (started_b, sid_b))

    # (3) The legitimate lifecycle succeeds: running -> children ->
    # complete + counters + seal in one UPDATE.
    config_c = _config("https://seed.example/c/")
    started_c = "2026-09-26T12:03:00+00:00"
    sid_c = wopaque("acq", entity, started_c, wsha(config_c))
    _insert_running(sid_c, started_c, config_c)
    page_url = "https://seed.example/c/home"
    content_sha = wsha("home page")
    evidence_id = wopaque("ev", sid_c, page_url, content_sha)
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,?,'usable',?,NULL,NULL,'text/html',?,NULL,?,?)",
        (evidence_id, sid_c, "src_official_web", page_url, "official",
         started_c, content_sha, wcanonical(
             {
                 "acquisition_kind": "bounded_official_website",
                 "entity_id": entity,
                 "start_url": "https://seed.example/c/",
                 "requested_url": page_url, "final_url": page_url,
                 "crawl_depth": 0, "page_role": "home", "home_page": True,
                 "business_wide_scope_eligible": True,
                 "crawl_frontier_exhausted": True, "title": None,
                 "canonical_url": page_url, "channels": [],
                 "observation_ids": [],
             }), started_c))
    conn.execute(
        "UPDATE acquisition_sessions SET status='complete', finished_at=?, "
        "error=NULL, evidence_count=1, observation_count=0, "
        "child_seal_sha256=? WHERE id=? AND status='running'",
        (started_c, session_child_seal_digest(conn, sid_c), sid_c))
    # Post-seal child append and counter mutation remain blocked.
    with pytest.raises(
        sqlite3.IntegrityError, match="sealed acquisition session"
    ):
        conn.execute(
            "INSERT INTO evidence_items("
            "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
            "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
            ") VALUES (?,?,?,'https://seed.example/c/x','official','usable',"
            "?,NULL,NULL,'text/html',?,NULL,'{}',?)",
            (wopaque("ev", sid_c, "https://seed.example/c/x", "x"),
             sid_c, "src_official_web", started_c, "x", started_c))
    with pytest.raises(
        sqlite3.IntegrityError, match="counters are immutable"
    ):
        conn.execute(
            "UPDATE acquisition_sessions SET evidence_count=2 WHERE id=?",
            (sid_c,))
    # Retro-sealing a terminal session is rejected: the OLD status is
    # already terminal, so the seal-set trigger refuses (pre-v5 history
    # is permanently unsealable).
    config_d = _config("https://seed.example/d/")
    started_d = "2026-09-26T12:04:00+00:00"
    sid_d = wopaque("acq", entity, started_d, wsha(config_d))
    # Simulate pre-v5 terminal history with a non-website collector
    # (schema-legal), then attempt to seal it: terminal OLD status is
    # refused by the seal-set trigger.
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid_d, entity, "src_official_web", "sara.other", "1",
         config_d, wsha(config_d), "complete", started_d, started_d,
         None, None, 0, 0))
    with pytest.raises(
        sqlite3.IntegrityError, match="never afterward"
    ):
        conn.execute(
            "UPDATE acquisition_sessions SET child_seal_sha256=? WHERE id=?",
            (session_child_seal_digest(conn, sid_d), sid_d))

    # (4) Producer-side wrong-digest lie: sealed via the legitimate
    # lifecycle with a fabricated digest; the reader recompute rejects.
    config_e = _config("https://seed.example/e/")
    started_e = "2026-09-26T12:05:00+00:00"
    sid_e = wopaque("acq", entity, started_e, wsha(config_e))
    _insert_running(sid_e, started_e, config_e)
    page_e = "https://seed.example/e/home"
    sha_e = wsha("e home")
    ev_e = wopaque("ev", sid_e, page_e, sha_e)
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,?,'usable',?,NULL,NULL,'text/html',?,NULL,?,?)",
        (ev_e, sid_e, "src_official_web", page_e, "official",
         started_e, sha_e, wcanonical(
             {
                 "acquisition_kind": "bounded_official_website",
                 "entity_id": entity,
                 "start_url": "https://seed.example/e/",
                 "requested_url": page_e, "final_url": page_e,
                 "crawl_depth": 0, "page_role": "home", "home_page": True,
                 "business_wide_scope_eligible": True,
                 "crawl_frontier_exhausted": True, "title": None,
                 "canonical_url": page_e, "channels": [],
                 "observation_ids": [],
             }), started_e))
    conn.execute(
        "UPDATE acquisition_sessions SET status='complete', finished_at=?, "
        "error=NULL, evidence_count=1, observation_count=0, "
        "child_seal_sha256=? WHERE id=? AND status='running'",
        (started_e, wsha("a producer-side lie"), sid_e))
    conn.commit()
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    invalid = {
        str(issue.get("session_id"))
        for issue in dossier["integrity_issues"]
        if issue["code"] == "customer_journey_website_session_invalid"
    }
    assert sid_e in invalid  # wrong digest caught by reader recompute
    assert sid_c not in invalid  # legitimate lifecycle clean
    used_sessions = {
        entry["acquisition_session_id"]
        for stage in dossier["customer_journey"]["stages"]
        for entry in stage["evidence"]
    }
    assert sid_c in used_sessions  # home row serves discover/evaluate
    assert sid_e not in used_sessions
    conn.close()

def test_day45_phone_channel_and_maps_evidence_retained(tmp_path: Path) -> None:
    """RCJ-04: at day 45 the phone fact (60-day window) is current; its
    channel and supporting Maps evidence must NOT be re-aged out by a
    universal 30-day window."""
    conn = prepared(tmp_path / "day45.sqlite")
    entity = business_entity_id_for_maps_business(1)
    evaluated = "2026-11-10T12:00:00+00:00"  # 45 days after Maps ingest
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at=evaluated)
    journey = dossier["customer_journey"]
    contact = next(s for s in journey["stages"] if s["stage"] == "contact")
    # phone fact: 60-day window → still fresh → contact observed.
    assert contact["evidence_state"] == "observed"
    phone_channel = next(
        c for c in contact["channels"] if c["channel_type"] == "phone")
    maps_rows = {
        row[0] for row in conn.execute(
            "SELECT e.id FROM evidence_items e "
            "WHERE e.source_id='src_google_maps'").fetchall()
    }
    assert phone_channel["evidence_id"] in maps_rows
    used = {entry["evidence_id"] for entry in contact["evidence"]}
    assert used & maps_rows, "Maps evidence retained for current phone fact"
    # The official-website fact carries a 90-day window: discover is
    # observed at day 45 through it — per-predicate windows, not one
    # universal clock.
    discover = next(s for s in journey["stages"] if s["stage"] == "discover")
    assert discover["evidence_state"] == "observed"
    conn.close()


def test_stale_stage_keeps_historical_handoffs(tmp_path: Path) -> None:
    """RCJ-05: a stale stage keeps its full historical surface, including
    booking/ordering/WhatsApp hand-offs, each flagged current=False."""
    conn = prepared(tmp_path / "stale-handoff.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=BOOKING_SITE)
    evaluated = "2027-01-15T12:00:00+00:00"  # everything aged out
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at=evaluated)
    journey = dossier["customer_journey"]
    book_order = next(s for s in journey["stages"] if s["stage"] == "book_order")
    assert book_order["evidence_state"] == "stale"
    booking_handoffs = [
        h for h in book_order["handoffs"] if h["to"] in ("booking", "ordering")
    ]
    assert booking_handoffs, "historical hand-offs retained on stale stage"
    for handoff in booking_handoffs:
        assert handoff["current"] is False
    doc_booking = [
        h for h in journey["handoffs"]
        if h["from"] == "official_website" and h["to"] in ("booking", "ordering")
    ]
    assert doc_booking
    assert all(h["current"] is False for h in doc_booking)
    # Sufficiency must not count historical hand-offs (policy filters
    # current=True) — the domain is stale here regardless.
    seal = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: evaluated)
    state = conn.execute(
        "SELECT state FROM dossier_domain_assessments "
        "WHERE assessment_id=? AND domain='customer_journey'",
        (seal.assessment_id,)).fetchone()[0]
    assert state == "stale"
    conn.close()


def test_maps_anchor_aggregation_not_fact_id_ordering(tmp_path: Path) -> None:
    """RCJ-06: discover currency follows whether ANY current Maps anchor
    exists — never opaque fact-id ordering. A stale name fact with a
    fresh phone fact yields observed regardless of id order."""
    from sara.dossier.customer_journey import reconstruct_customer_journey

    conn = prepared(tmp_path / "anchors.sqlite")
    entity = business_entity_id_for_maps_business(1)
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    facts = [dict(f) for f in dossier["facts"]]
    # Make name/address/lat/lon stale (90d window, aged out) while phone
    # (60d) stays fresh — mixed anchor freshness.
    stale_predicates = {
        "business.name.trading", "location.address",
        "location.latitude", "location.longitude",
    }
    for fact in facts:
        if fact["predicate"] in stale_predicates:
            fact["freshness"] = dict(fact["freshness"], is_stale=True)
    doc, issues = reconstruct_customer_journey(
        conn, entity_id=entity, facts=facts, evidence=dossier["evidence"],
        evaluated_at="2026-09-26T12:30:00+00:00")
    discover = next(s for s in doc["stages"] if s["stage"] == "discover")
    assert discover["evidence_state"] == "observed"
    assert issues == []
    # And the mirror: only-stale anchors → stale, never unknown.
    facts_all_stale = [dict(f) for f in facts]
    for fact in facts_all_stale:
        if fact["predicate"] in stale_predicates | {
            "location.phone", "business.website.official",
        }:
            fact["freshness"] = dict(fact["freshness"], is_stale=True)
    doc2, _ = reconstruct_customer_journey(
        conn, entity_id=entity, facts=facts_all_stale,
        evidence=dossier["evidence"],
        evaluated_at="2026-09-26T12:30:00+00:00")
    discover2 = next(s for s in doc2["stages"] if s["stage"] == "discover")
    assert discover2["evidence_state"] == "stale"
    conn.close()


def test_explicit_stale_status_fact_remains_contribution(tmp_path: Path) -> None:
    """Schema edge: a value-bearing status='stale' fact is history Sara
    stands behind — it contributes with current=False, marking the stage
    stale rather than unknown."""
    from sara.dossier.customer_journey import reconstruct_customer_journey

    conn = prepared(tmp_path / "stale-status.sqlite")
    entity = business_entity_id_for_maps_business(1)
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    facts = [dict(f) for f in dossier["facts"]]
    for fact in facts:
        if fact["predicate"] == "location.phone":
            fact["status"] = "stale"
    doc, _ = reconstruct_customer_journey(
        conn, entity_id=entity, facts=facts, evidence=dossier["evidence"],
        evaluated_at="2026-09-26T12:30:00+00:00")
    contact = next(s for s in doc["stages"] if s["stage"] == "contact")
    assert contact["evidence_state"] == "stale"
    assert contact["evidence"], "historical surface retained"
    conn.close()



# ---- R1-R4 decisive regressions (frozen review, exact head fd8a2e0) ----

def test_full_shape_booking_child_with_empty_observations_invalid(tmp_path: Path) -> None:
    """R1/XJ-01 (decisive): the full-shape booking child with empty
    observations cannot even be INSERTED under a sealed session — and a
    hand-built session carrying that shape is rejected by the row
    semantic seal."""
    import sqlite3

    conn = prepared(tmp_path / "forged3.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = acquire(conn, tmp_path, pages=BOOKING_SITE)
    session_id = stats.session_id
    from sara.website.model import canonical_json as wcanonical
    from sara.website.model import opaque_id as wopaque, sha256_text as wsha

    forged_url = "https://seed.example/forged3"
    forged_sha = wsha("forged3 body")
    forged_id = wopaque("ev", session_id, forged_url, forged_sha)
    metadata = wcanonical(
        {
            "acquisition_kind": "bounded_official_website",
            "entity_id": entity,
            "start_url": "https://seed.example/",
            "requested_url": forged_url,
            "final_url": forged_url,
            "crawl_depth": 0,
            "page_role": "booking",
            "home_page": False,
            "business_wide_scope_eligible": True,
            "crawl_frontier_exhausted": True,
            "title": None,
            "canonical_url": forged_url,
            "channels": [{
                "channel_type": "booking",
                "identifier": "https://booksy.com/forged3",
                "normalized_identifier": "https://booksy.com/forged3",
                "url": "https://booksy.com/forged3",
                "extraction": "action_link",
                "canonicalized_channel_id": None,
                "canonicalized_scope": None,
            }],
            "observation_ids": [],
        }
    )
    with pytest.raises(sqlite3.IntegrityError, match="sealed acquisition session"):
        conn.execute(
            "INSERT INTO evidence_items("
            "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
            "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
            ") VALUES (?,?,?,'https://seed.example/forged3','official','usable',"
            "?,NULL,NULL,'text/html',?,NULL,?,?)",
            (forged_id, session_id, "src_official_web",
             "2026-09-26T12:00:09+00:00", forged_sha, metadata,
             "2026-09-26T12:00:09+00:00"))
    # Reader semantic seal (independent of membership): a booking
    # channel with ZERO attached observations is impossible producer
    # output. The row is sealed through the LEGITIMATE lifecycle
    # (running -> children -> complete+counters+seal) so membership
    # passes — only the semantic check can reject it.
    from sara.storage import session_child_seal_digest

    config = wcanonical(
        {
            "entity_id": entity, "start_url": "https://hand.example/",
            "page_limit": 8, "depth_limit": 2,
            "max_response_bytes": 1048576, "timeout_seconds": 10.0,
            "request_interval_seconds": 1.0,
            "max_policy_delay_seconds": 30.0, "retry_attempt_limit": 4,
            "retry_base_delay_seconds": 1.0, "retry_max_delay_seconds": 30.0,
            "retry_delay_budget_seconds": 60.0,
            "user_agent": "SaraBusinessUnderstanding/1.0",
            "obey_robots": True, "evidence_root": "/tmp/ev",
        }
    )
    hand_started = "2026-09-26T12:15:00+00:00"
    hand_session = wopaque("acq", entity, hand_started, wsha(config))
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,'running',?,NULL,NULL,NULL,0,0)",
        (hand_session, entity, "src_official_web", "sara.website", "5",
         config, wsha(config), hand_started))
    hand_url = "https://hand.example/book"
    hand_sha = wsha("hand booking page")
    hand_ev = wopaque("ev", hand_session, hand_url, hand_sha)
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,?,'usable',?,NULL,NULL,'text/html',?,NULL,?,?)",
        (hand_ev, hand_session, "src_official_web", hand_url, "official",
         hand_started, hand_sha, wcanonical(
             {
                 "acquisition_kind": "bounded_official_website",
                 "entity_id": entity,
                 "start_url": "https://hand.example/",
                 "requested_url": hand_url, "final_url": hand_url,
                 "crawl_depth": 0, "page_role": "booking",
                 "home_page": False, "business_wide_scope_eligible": True,
                 "crawl_frontier_exhausted": True, "title": None,
                 "canonical_url": hand_url,
                 "channels": [{
                     "channel_type": "booking",
                     "identifier": "https://booksy.com/hand",
                     "normalized_identifier": "https://booksy.com/hand",
                     "url": "https://booksy.com/hand",
                     "extraction": "action_link",
                     "canonicalized_channel_id": None,
                     "canonicalized_scope": None,
                 }],
                 "observation_ids": [],
             }), hand_started))
    conn.execute(
        "UPDATE acquisition_sessions SET status='complete', finished_at=?, "
        "error=NULL, evidence_count=1, observation_count=0, "
        "child_seal_sha256=? WHERE id=? AND status='running'",
        (hand_started, session_child_seal_digest(conn, hand_session),
         hand_session))
    conn.commit()
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    # Membership passed (no session-level flag); the SEMANTIC seal flags
    # the row itself.
    assert not any(
        i["code"] == "customer_journey_website_session_invalid"
        and i["session_id"] == hand_session
        for i in dossier["integrity_issues"]
    )
    semantic_issue = next(
        (i for i in dossier["integrity_issues"]
         if i["code"] == "customer_journey_website_evidence_invalid"
         and i["evidence_id"] == hand_ev), None)
    assert semantic_issue is not None
    channels = {
        (c["identifier"], c["normalized_identifier"])
        for stage in dossier["customer_journey"]["stages"]
        for c in stage["channels"]
    }
    assert ("https://booksy.com/hand", "https://booksy.com/hand") not in (
        channels
    )
    assert not any(
        h["evidence_id"] == hand_ev
        for h in dossier["customer_journey"]["handoffs"]
    )
    conn.close()

def test_absence_on_complete_nonexhausted_session_not_bounded(tmp_path: Path) -> None:
    """R2: absence support bound to the producer's actual contract — a
    genuine v5 complete, scope-eligible session with
    crawl_frontier_exhausted=False cannot establish journey bounded
    not-observed."""
    from sara.dossier.customer_journey import reconstruct_customer_journey
    from sara.website.model import (
        canonical_json as wcanonical,
        opaque_id as wopaque,
        sha256_text as wsha,
    )

    conn = prepared(tmp_path / "absence2.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = acquire(conn, tmp_path, pages=MENU_ONLY_SITE)
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    absence_fact = next(
        fact for fact in dossier["facts"]
        if fact["predicate"] == "capability.online_booking"
        and fact["status"] == "not_observed")
    support = absence_fact["acquisition_support"][0]

    # A producer-valid v5 complete scope-eligible session whose crawl was
    # budget-truncated: frontier_exhausted=False on every row. The
    # session id is the PRODUCER-DETERMINISTIC opaque_id over
    # (entity, started_at, config_hash) — XJ-04: the exclusion must be
    # attributable to the frontier gate, never to id verification.
    config = wcanonical(
        {
            "entity_id": entity,
            "start_url": "https://seed.example/",
            "page_limit": 8, "depth_limit": 2,
            "max_response_bytes": 1048576, "timeout_seconds": 10.0,
            "request_interval_seconds": 1.0,
            "max_policy_delay_seconds": 30.0,
            "retry_attempt_limit": 4, "retry_base_delay_seconds": 1.0,
            "retry_max_delay_seconds": 30.0,
            "retry_delay_budget_seconds": 60.0,
            "user_agent": "SaraBusinessUnderstanding/1.0",
            "obey_robots": True,
            "evidence_root": "/tmp/ev",
        }
    )
    started_at = "2026-09-26T12:10:00+00:00"
    forged_session = wopaque("acq", entity, started_at, wsha(config))
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,'running',?,NULL,NULL,NULL,0,0)",
        (forged_session, entity, "src_official_web", "sara.website", "5",
         config, wsha(config), started_at))
    page_url = "https://seed.example/"
    content_sha = wsha("home")
    evidence_id = wopaque("ev", forged_session, page_url, content_sha)
    metadata = wcanonical(
        {
            "acquisition_kind": "bounded_official_website",
            "entity_id": entity,
            "start_url": page_url,
            "requested_url": page_url,
            "final_url": page_url,
            "crawl_depth": 0,
            "page_role": "home",
            "home_page": True,
            "business_wide_scope_eligible": True,
            "crawl_frontier_exhausted": False,  # budget-truncated
            "title": None,
            "canonical_url": page_url,
            "channels": [],
            "observation_ids": [],
        }
    )
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,?,'usable',?,NULL,NULL,'text/html',?,NULL,?,?)",
        (evidence_id, forged_session, "src_official_web", page_url,
         "official", started_at, content_sha, metadata, started_at))
    from sara.storage import session_child_seal_digest

    conn.execute(
        "UPDATE acquisition_sessions SET status='complete', finished_at=?, "
        "error=NULL, evidence_count=1, observation_count=0, "
        "child_seal_sha256=? WHERE id=? AND status='running'",
        (started_at, session_child_seal_digest(conn, forged_session),
         forged_session))
    conn.commit()

    facts = [dict(f) for f in dossier["facts"]]
    for index, fact in enumerate(facts):
        if fact["id"] == absence_fact["id"]:
            patched = dict(fact)
            patched_support = dict(support)
            patched_support["acquisition_session_id"] = forged_session
            patched["acquisition_support"] = [patched_support]
            facts[index] = patched
    doc, issues = reconstruct_customer_journey(
        conn, entity_id=entity, facts=facts, evidence=dossier["evidence"],
        evaluated_at="2026-09-26T12:30:00+00:00")
    # The session is fully producer-valid: NO integrity issue flags it.
    assert not any(
        "session" in str(issue.get("code")) or "evidence" in str(
            issue.get("code"))
        for issue in issues
    ), issues
    # Its positive evidence IS admitted — the home row serves discover
    # and evaluate — proving admission passed id/config/seal checks and
    # ONLY the absence gate is under test.
    used_sessions = {
        entry["acquisition_session_id"]
        for stage in doc["stages"]
        for entry in stage["evidence"]
    }
    assert forged_session in used_sessions
    book_order = next(s for s in doc["stages"] if s["stage"] == "book_order")
    assert book_order["evidence_state"] == "unknown"
    assert book_order["evidence_state"] != "not_observed_in_bounded_inspection"
    conn.close()


def test_domain_move_never_reclassifies_old_same_site_link(tmp_path: Path) -> None:
    """R3: a historical booking link that was SAME-SITE at source time
    (old.example/book on old.example) must not become an external
    hand-off after the business moves to a new domain — even while the
    old evidence is still inside the currency window."""
    from sara.website.model import (
        canonical_json as wcanonical,
        opaque_id as wopaque,
        sha256_text as wsha,
    )

    conn = prepared(tmp_path / "domainmove.sqlite")
    entity = business_entity_id_for_maps_business(1)
    # Current, real crawl on seed.example: today's website fact host.
    acquire(conn, tmp_path, pages=MENU_ONLY_SITE)
    # A producer-valid historical session on the OLD domain whose booking
    # link is SAME-SITE under that old domain.
    config = wcanonical(
        {
            "entity_id": entity,
            "start_url": "https://old.example/",
            "page_limit": 8, "depth_limit": 2,
            "max_response_bytes": 1048576, "timeout_seconds": 10.0,
            "request_interval_seconds": 1.0,
            "max_policy_delay_seconds": 30.0,
            "retry_attempt_limit": 4, "retry_base_delay_seconds": 1.0,
            "retry_max_delay_seconds": 30.0,
            "retry_delay_budget_seconds": 60.0,
            "user_agent": "SaraBusinessUnderstanding/1.0",
            "obey_robots": True,
            "evidence_root": "/tmp/ev",
        }
    )
    started_at = "2026-09-26T12:20:00+00:00"
    old_session = wopaque("acq", entity, started_at, wsha(config))
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,'running',?,NULL,NULL,NULL,0,0)",
        (old_session, entity, "src_official_web", "sara.website", "5",
         config, wsha(config), started_at))
    page_url = "https://old.example/book"
    content_sha = wsha("old booking page")
    evidence_id = wopaque("ev", old_session, page_url, content_sha)
    observation_json = wcanonical(True)
    observation_id = wopaque(
        "obs", evidence_id, "capability.online_booking",
        wsha(observation_json))
    metadata = wcanonical(
        {
            "acquisition_kind": "bounded_official_website",
            "entity_id": entity,
            "start_url": "https://old.example/",
            "requested_url": page_url,
            "final_url": page_url,
            "crawl_depth": 0,
            "page_role": "booking",
            "home_page": False,
            "business_wide_scope_eligible": True,
            "crawl_frontier_exhausted": True,
            "title": None,
            "canonical_url": page_url,
            "channels": [{
                "channel_type": "booking",
                "identifier": "https://old.example/book",
                "normalized_identifier": "https://old.example/book",
                "url": "https://old.example/book",
                "extraction": "action_link",
                "canonicalized_channel_id": None,
                "canonicalized_scope": None,
            }],
            "observation_ids": [observation_id],
        }
    )
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,?,?,'usable',?,NULL,NULL,'text/html',?,NULL,?,?)",
        (evidence_id, old_session, "src_official_web", page_url, "official",
         started_at, content_sha, metadata, started_at))
    conn.execute(
        "INSERT INTO observations("
        "id,subject_id,predicate,evidence_id,value_json,"
        "normalized_value_json,value_hash,observation_kind,"
        "observed_at,extracted_at,extraction_method,extractor_name,"
        "extractor_version,confidence,created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (observation_id, entity, "capability.online_booking", evidence_id,
         observation_json, observation_json, wsha(observation_json),
         "detected_capability", started_at, started_at, "heuristic",
         "sara.website", "5", 0.8, started_at))
    from sara.storage import session_child_seal_digest

    conn.execute(
        "UPDATE acquisition_sessions SET status='complete', finished_at=?, "
        "error=NULL, evidence_count=1, observation_count=1, "
        "child_seal_sha256=? WHERE id=? AND status='running'",
        (started_at, session_child_seal_digest(conn, old_session),
         old_session))
    conn.commit()
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    journey = dossier["customer_journey"]
    # The old evidence IS admitted and current (booking channel present on
    # the book_order stage)...
    book_order = next(s for s in journey["stages"] if s["stage"] == "book_order")
    assert book_order["evidence_state"] == "observed"
    assert any(
        c["normalized_identifier"] == "https://old.example/book"
        for c in book_order["channels"]
    )
    # ...but NO hand-off to old.example/book exists: at source time it was
    # a same-site action link, and today's seed.example fact must not
    # reclassify it as an external booking endpoint.
    assert not any(
        h["from"] == "official_website"
        and h["to"] == "booking"
        and h["evidence_id"] == evidence_id
        for h in journey["handoffs"]
    )
    conn.close()


# planner boundary: no journey acquisition action in this PR
def test_customer_journey_remains_unsupported_for_planner() -> None:
    assert "customer_journey" in UNSUPPORTED_DOMAINS


# sufficiency end-to-end: entry + action + later stage
def test_sufficient_journey_unblocks_domain(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "sufficient.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=SUPPORT_SITE)
    seal = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:30:00+00:00")
    row = conn.execute(
        "SELECT state, reason_json FROM dossier_domain_assessments "
        "WHERE assessment_id=? AND domain='customer_journey'",
        (seal.assessment_id,)).fetchone()
    assert row[0] == "sufficient"
    reason = json.loads(row[1])
    assert reason["code"] == "observable_journey_stages_reconstructed"
    assert "customer_journey" not in seal.blocking_mandatory_domains
    conn.close()


# ---- v3 coverage-based sufficiency (public_surface_coverage) ----
# Rule: entry/evaluation stage + customer-action stage + public-surface
# coverage (current evaluate evidence, or a sealed bounded inspection
# with no evaluate surface). Later stages and hand-offs are evidence,
# never a gate. The reconstruction owns the coverage judgment.

# Deep start whose origin root is unreachable: the crawler enqueues the
# root for any non-root start, so the root failure forces a partial
# session (the production B1 mechanics: scope can never be established
# by a root that was never retained).
DEEP_LANDING_SITE_BROKEN = {
    "https://seed.example/landing/start": """
        <link rel="canonical" href="https://seed.example/">
        <a href="/landing/details">Details</a>
    """,
}

DEEP_LANDING_SITE_NO_CANONICAL = {
    "https://seed.example/landing/start": """
        <a href="/landing/details">Details</a>
    """,
}

# Root that redirects to a deep, non-evaluative landing: a root start is
# business-wide scope eligible by construction, and the retained page's
# role follows the FINAL url, so a complete frontier-exhausted crawl can
# legitimately carry no evaluate-qualifying page.
ROOT_REDIRECT_ROUTE = {
    "https://seed.example/": (
        "https://seed.example/landing/start",
        """<a href="/landing/details">Details</a>""",
    ),
    "https://seed.example/landing/start": (
        "https://seed.example/landing/start",
        """<a href="/landing/details">Details</a>""",
    ),
    "https://seed.example/landing/details": (
        "https://seed.example/landing/details",
        "",
    ),
}


def _redirect_client_factory(route):
    class RedirectClient:
        def fetch(self, url: str) -> HttpResponse:
            if url not in route:
                raise WebsiteFetchError(f"HTTP 404 for {url}")
            final_url, body = route[url]
            return HttpResponse(
                requested_url=url, final_url=final_url, status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=body.encode(),
                media_type="text/html", charset="utf-8",
            )
    client = RedirectClient()
    return lambda **_kw: client


def _coverage(conn, entity, evaluated_at="2026-09-26T12:30:00+00:00"):
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at=evaluated_at)
    return dossier, dossier["customer_journey"]["public_surface_coverage"]


def _journey_state(conn, entity, now="2026-09-26T12:30:00+00:00"):
    seal = persist_dossier_assessment(conn, entity_id=entity, now=lambda: now)
    row = conn.execute(
        "SELECT state, reason_json FROM dossier_domain_assessments "
        "WHERE assessment_id=? AND domain='customer_journey'",
        (seal.assessment_id,)).fetchone()
    return row[0], json.loads(row[1])


# decisive case 1: evaluate evidence + action => sufficient (v2 said
# partial on later_stage_or_action_handoff for exactly this shape)
def test_coverage_menu_only_site_is_sufficient(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "cov-menu.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=MENU_ONLY_SITE)
    _, coverage = _coverage(conn, entity)
    assert coverage["state"] == "evaluation_observed"
    assert coverage["evidence_ids"] and coverage["session_ids"] == []
    assert {item["evidence_id"] for item in coverage["support"]} == set(
        coverage["evidence_ids"])
    state, reason = _journey_state(conn, entity)
    assert state == "sufficient"
    assert reason["code"] == "observable_journey_stages_reconstructed"
    assert reason["public_surface_coverage_state"] == "evaluation_observed"
    assert reason["derivation_version"] == "dossier-assessment-v3"
    # invariants: sufficiency never manufactures later stages
    _, journey = _journey(conn, entity)
    assert _states(journey)["pay"] == "unknown"
    assert _states(journey)["receive"] == "unknown"
    conn.close()


# decisive case 2: bounded inspection with no evaluate surface + action
# => sufficient through the coverage fallback (root redirect to a deep,
# non-evaluative landing; complete, scope-eligible, frontier-exhausted)
def test_coverage_bounded_inspection_without_evaluate_is_sufficient(
        tmp_path: Path) -> None:
    conn = prepared(tmp_path / "cov-bounded.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = acquire(conn, tmp_path, pages=None,
                    client_factory=_redirect_client_factory(ROOT_REDIRECT_ROUTE))
    assert stats.status == "complete"
    assert stats.crawl_frontier_exhausted is True
    _, journey = _journey(conn, entity)
    assert _states(journey)["evaluate"] == "unknown"
    coverage = journey["public_surface_coverage"]
    assert coverage["state"] == "bounded_inspection_no_evaluation"
    assert coverage["session_ids"] == [stats.session_id]
    assert coverage["evidence_ids"] == []
    assert {item["acquisition_session_id"]
            for item in coverage["support"]} == {stats.session_id}
    state, reason = _journey_state(conn, entity)
    assert state == "sufficient"
    assert reason["public_surface_coverage_state"] == (
        "bounded_inspection_no_evaluation")
    assert _states(journey)["pay"] == "unknown"
    assert _states(journey)["receive"] == "unknown"
    conn.close()


# decisive case 3a: partial crawl without evaluate surface stays partial
def test_coverage_partial_crawl_no_evaluate_stays_partial(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "cov-partial.sqlite",
                    website="https://seed.example/landing/start")
    entity = business_entity_id_for_maps_business(1)
    stats = acquire(conn, tmp_path, pages=DEEP_LANDING_SITE_BROKEN)
    assert stats.status == "partial"
    _, coverage = _coverage(conn, entity)
    assert coverage["state"] == "not_covered"
    state, reason = _journey_state(conn, entity)
    assert state == "partial"
    assert reason["unmet_conditions"] == ["public_surface_coverage"]
    conn.close()


# decisive case 3b: deep start with unreachable root is partial and
# uncovered regardless of a declared canonical (the production B1 shape)
def test_coverage_scope_ineligible_no_evaluate_stays_partial(
        tmp_path: Path) -> None:
    conn = prepared(tmp_path / "cov-scope.sqlite",
                    website="https://seed.example/landing/start")
    entity = business_entity_id_for_maps_business(1)
    stats = acquire(conn, tmp_path, pages=DEEP_LANDING_SITE_NO_CANONICAL)
    assert stats.status == "partial"
    _, coverage = _coverage(conn, entity)
    assert coverage["state"] == "not_covered"
    state, _ = _journey_state(conn, entity)
    assert state == "partial"
    conn.close()


# decisive case 4: website known but never inspected stays partial (B2)
def test_coverage_known_website_never_inspected_stays_partial(
        tmp_path: Path) -> None:
    conn = prepared(tmp_path / "cov-known.sqlite")
    entity = business_entity_id_for_maps_business(1)
    _, coverage = _coverage(conn, entity)
    assert coverage["state"] == "not_covered"
    assert coverage["evidence_ids"] == [] and coverage["session_ids"] == []
    state, reason = _journey_state(conn, entity)
    assert state == "partial"
    assert reason["unmet_conditions"] == ["public_surface_coverage"]
    conn.close()


# decisive case 5: Maps + phone only stays partial (C)
def test_coverage_maps_phone_only_stays_partial(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "cov-maps-only.sqlite", website=None)
    entity = business_entity_id_for_maps_business(1)
    _, coverage = _coverage(conn, entity)
    assert coverage["state"] == "not_covered"
    state, reason = _journey_state(conn, entity)
    assert state == "partial"
    assert reason["unmet_conditions"] == ["public_surface_coverage"]
    conn.close()


# decisive case 6: evaluate without an action stage stays partial; a
# bounded book_order absence never satisfies the action condition
def test_coverage_evaluate_without_action_stays_partial(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "cov-no-action.sqlite", phone=None)
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=MENU_ONLY_SITE)
    _, coverage = _coverage(conn, entity)
    assert coverage["state"] == "evaluation_observed"
    _, journey = _journey(conn, entity)
    assert _states(journey)["contact"] == "unknown"
    assert _states(journey)["book_order"] == (
        "not_observed_in_bounded_inspection")
    state, reason = _journey_state(conn, entity)
    assert state == "partial"
    assert reason["unmet_conditions"] == ["customer_action_stage"]
    conn.close()


# coverage determinism and currency: same inputs, same judgment; a
# bounded inspection outside the freshness window no longer covers
def test_coverage_deterministic_and_staleness_fail_closed(
        tmp_path: Path) -> None:
    conn = prepared(tmp_path / "cov-det.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=None,
            client_factory=_redirect_client_factory(ROOT_REDIRECT_ROUTE))
    first = _coverage(conn, entity)[1]
    second = _coverage(conn, entity)[1]
    assert first == second
    assert first["state"] == "bounded_inspection_no_evaluation"
    stale = _coverage(conn, entity, "2026-11-20T12:00:00+00:00")[1]
    assert stale["state"] == "not_covered"
    assert stale["session_ids"] == []
    conn.close()


# v3 identity: the sealed snapshot stamps the new derivation version
def test_v3_derivation_version_sealed_in_assessment(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "cov-v3.sqlite")
    entity = business_entity_id_for_maps_business(1)
    acquire(conn, tmp_path, pages=MENU_ONLY_SITE)
    seal = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:30:00+00:00")
    summary = json.loads(conn.execute(
        "SELECT summary_json FROM dossier_assessments WHERE id=?",
        (seal.assessment_id,)).fetchone()[0])
    assert summary["derivation_version"] == "dossier-assessment-v3"
    assert "customer_journey" not in seal.blocking_mandatory_domains
    conn.close()


# PCJ-01: a bounded session whose rows straddle the freshness boundary is
# not current coverage. Stale evaluate-qualifying rows must not convert
# into a current negative judgment through a younger non-evaluative row
# from the same crawl.
MENU_AND_DETAILS_SITE = {
    "https://seed.example/": """
        <link rel="canonical" href="https://seed.example/">
        <a href="/menu">Menu</a>
        <a href="/details">Details</a>
    """,
    "https://seed.example/menu": "<h1>Grill and Mezzes</h1>",
    "https://seed.example/details": "<p>Plain page</p>",
}


def test_pcj01_boundary_straddling_session_not_covered(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "pcj01.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = acquire(conn, tmp_path, pages=MENU_AND_DETAILS_SITE)
    assert stats.status == "complete"
    assert stats.crawl_frontier_exhausted is True
    role_times: dict[str, list[datetime]] = {}
    for retrieved_at, metadata_json in conn.execute(
        "SELECT retrieved_at, metadata_json FROM evidence_items "
        "WHERE acquisition_session_id=?",
        (stats.session_id,),
    ):
        role = json.loads(metadata_json).get("page_role")
        role_times.setdefault(str(role), []).append(
            datetime.fromisoformat(retrieved_at))
    evaluative = sorted(role_times["home"] + role_times["offerings"])
    plain = min(role_times["other"])
    assert max(evaluative) < plain, "fixture must make evaluate rows older"
    # evaluate rows fall outside the 30-day window; the plain row stays
    # inside it — exactly the existential-currency loophole
    evaluated = (
        max(evaluative) + timedelta(days=30) + (plain - max(evaluative)) / 2
    )
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at=evaluated.isoformat())
    journey = dossier["customer_journey"]
    evaluate_stage = next(
        s for s in journey["stages"] if s["stage"] == "evaluate")
    assert evaluate_stage["evidence_state"] == "stale"
    coverage = journey["public_surface_coverage"]
    assert coverage["state"] == "not_covered"
    assert coverage["session_ids"] == [] and coverage["support"] == []
    conn.close()


# PCJ-02: coverage-supporting website rows that generate no fact and
# belong to no stage still reach the sealed chronology and facts_as_of.
def test_pcj02_coverage_only_rows_reach_chronology(tmp_path: Path) -> None:
    from sara.dossier.assessment import (
        _chronology_inputs,
        _customer_journey_signature,
        _location_owner_resolution_state,
        _provenance_record_state,
        _structural_subject_state,
    )

    conn = prepared(tmp_path / "pcj02.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = acquire(conn, tmp_path, pages=None,
                    client_factory=_redirect_client_factory(
                        ROOT_REDIRECT_ROUTE))
    assert stats.status == "complete"
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    journey = dossier["customer_journey"]
    coverage = journey["public_surface_coverage"]
    assert coverage["state"] == "bounded_inspection_no_evaluation"
    assert coverage["support"], "bounded coverage must expose provenance"
    row_ids = {row[0] for row in conn.execute(
        "SELECT id FROM evidence_items WHERE acquisition_session_id=?",
        (stats.session_id,))}
    assert {item["evidence_id"] for item in coverage["support"]} == row_ids
    assert all(
        set(item) == {
            "evidence_id", "acquisition_session_id", "retrieved_at",
        }
        for item in coverage["support"]
    )

    # the sealed journey signature carries the provenance rows
    sealed = _customer_journey_signature(journey)["public_surface_coverage"]
    assert sealed["support"] == sorted(
        coverage["support"], key=lambda item: item["evidence_id"])

    # chronology includes a coverage-labeled entry per support row, and
    # the persisted watermark is not earlier than the newest such row
    chronology = _chronology_inputs(
        dossier,
        _location_owner_resolution_state(conn, dossier),
        _structural_subject_state(conn, dossier),
        _provenance_record_state(conn, dossier),
    )
    labels = {str(item["field"]) for item in chronology}
    journey_labels = [l for l in labels if l.startswith("customer journey")]
    stage_evidence_ids = {
        str(entry.get("evidence_id"))
        for stage in journey["stages"]
        for entry in stage.get("evidence", ())
    }
    for item in coverage["support"]:
        # every coverage-supporting row is incorporated under SOME
        # journey chronology label (stage evidence or coverage)
        assert any(item["evidence_id"] in l for l in journey_labels), item
    coverage_only = (
        {item["evidence_id"] for item in coverage["support"]}
        - stage_evidence_ids
    )
    assert coverage_only, "fixture must include a stage-less coverage row"
    for evidence_id in coverage_only:
        # rows that no stage claims enter through the coverage route only
        assert any(
            evidence_id in l and "public_surface_coverage" in l
            for l in journey_labels
        ), evidence_id
    newest_row = max(
        datetime.fromisoformat(item["retrieved_at"])
        for item in coverage["support"]
    )
    seal = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:30:00+00:00")
    facts_as_of = conn.execute(
        "SELECT facts_as_of FROM dossier_assessments WHERE id=?",
        (seal.assessment_id,)).fetchone()[0]
    assert datetime.fromisoformat(facts_as_of) >= newest_row
    conn.close()


# SR-01: snapshots sealed under a superseded policy identity are never
# surfaced as the persisted current policy, regardless of computed_at.
def test_sr01_superseded_policy_snapshot_not_current(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "sr01.sqlite")
    entity = business_entity_id_for_maps_business(1)

    def persisted_current(evaluated_at="2026-09-26T12:30:00+00:00"):
        dossier = build_business_dossier(
            conn, entity_id=entity, evaluated_at=evaluated_at)
        return dossier["dossier_status"]["persisted_current_policy"]

    # an old-policy-only history: sealed, but never the current policy
    with patch("sara.dossier.assessment.DOSSIER_POLICY_VERSION",
               "business-understanding-v1"):
        old = persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:00:00+00:00")
    assert old.assessment_id
    assert persisted_current() is None

    # the active-policy snapshot is surfaced
    new = persist_dossier_assessment(
        conn, entity_id=entity, now=lambda: "2026-09-26T12:10:00+00:00")
    current = persisted_current()
    assert current is not None
    assert current["id"] == new.assessment_id
    assert current["policy_version"] == "business-understanding-v2"
    assert current["summary"]["derivation_version"] == "dossier-assessment-v3"

    # a LATER old-policy write (freshness advanced by a real crawl) must
    # not shadow the current-policy snapshot
    crawl_clock = Clock()
    crawl_clock.current = datetime.fromisoformat(
        "2026-09-26T12:20:00+00:00")
    acquire(conn, tmp_path, pages=MENU_ONLY_SITE, clock=crawl_clock)
    with patch("sara.dossier.assessment.DOSSIER_POLICY_VERSION",
               "business-understanding-v1"):
        old_later = persist_dossier_assessment(
            conn, entity_id=entity, now=lambda: "2026-09-26T12:40:00+00:00")
    assert old_later.assessment_id not in {
        old.assessment_id, new.assessment_id}
    current = persisted_current()
    assert current is not None
    assert current["policy_version"] == "business-understanding-v2"
    conn.close()


def _supersede_website_fact(conn, entity, new_website, at):
    """Supersede the open official-website fact through the reconciler's
    own row mutations (close the open fact, insert the successor)."""
    import hashlib

    from sara.website.reconcile import _close_fact, _insert_fact

    row = conn.execute(
        "SELECT id, fact_slot FROM facts "
        "WHERE subject_id=? AND predicate='business.website.official' "
        "AND valid_to IS NULL ORDER BY created_at DESC LIMIT 1",
        (entity,),
    ).fetchone()
    assert row is not None, "expected an open website fact"
    _close_fact(conn, row[0], valid_to=at)
    value_json = json.dumps(new_website)
    fact_id = "ft_" + hashlib.sha256(
        (entity + new_website + at).encode("utf-8")).hexdigest()[:32]
    _insert_fact(
        conn, fact_id=fact_id, entity_id=entity,
        predicate="business.website.official", fact_slot=row[1],
        value_json=value_json,
        value_hash=hashlib.sha256(value_json.encode("utf-8")).hexdigest(),
        status="single_source", valid_from=at, reconciled_at=at,
    )
    conn.commit()


# SR-02: a bounded crawl of a superseded site must not establish coverage
# for a different current official website; a same-site change under
# crawler host semantics (www variant, deep path) keeps legitimate
# bounded coverage.
def test_sr02_bounded_coverage_bound_to_current_site(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "sr02.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = acquire(conn, tmp_path, pages=None,
                    client_factory=_redirect_client_factory(
                        ROOT_REDIRECT_ROUTE))
    assert stats.status == "complete"

    def domain_state():
        dossier = build_business_dossier(
            conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
        coverage = dossier["customer_journey"]["public_surface_coverage"]
        assessments = {
            str(a["domain"]): a
            for a in derive_domain_assessments(dossier)
        }
        return coverage, assessments["customer_journey"]

    coverage, journey_domain = domain_state()
    assert coverage["state"] == "bounded_inspection_no_evaluation"
    assert journey_domain["state"] == "sufficient"

    # same site, different host spelling and path: coverage survives
    _supersede_website_fact(
        conn, entity, "https://www.seed.example/landing/elsewhere",
        at="2026-09-26T11:00:00+00:00")
    coverage, journey_domain = domain_state()
    assert coverage["state"] == "bounded_inspection_no_evaluation"
    assert journey_domain["state"] == "sufficient"

    # a different site that was never crawled: the bounded crawl of the
    # superseded site no longer speaks for the public surface
    _supersede_website_fact(
        conn, entity, "https://other.example/",
        at="2026-09-26T11:30:00+00:00")
    coverage, journey_domain = domain_state()
    assert coverage["state"] == "not_covered"
    assert coverage["session_ids"] == [] and coverage["support"] == []
    assert journey_domain["state"] == "partial"
    assert journey_domain["reason"]["unmet_conditions"] == [
        "public_surface_coverage"]
    conn.close()
