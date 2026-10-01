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


def prepared(path: Path, *, reviews=None):
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
    record = {
        "place_id": "place-journey", "cid": "cid-journey", "data_id": "data-journey",
        "title": "Journey Business", "category": "Restaurant",
        "address": "Journey Street", "latitude": 21.55, "longitude": 39.18,
        "phone": "+966500000000", "website": "https://seed.example/",
        "review_rating": 4.4, "review_count": 12, "status": "Open",
        "link": "https://maps.example/journey",
    }
    if reviews is not None:
        record["user_reviews"] = reviews
    with patch("sara.storage.utc_now", return_value="2026-09-26T09:59:00+00:00"), patch(
        "sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"
    ):
        ingest_records(conn, "r1", [record], finalize_run=("complete", 0, None))
        backfill_maps_business_understanding(conn)
    return conn


def acquire(conn, tmp_path, *, pages, page_limit=8, clock=None):
    return collect_official_website(
        conn, evidence_root=tmp_path / "ev", business_id=1,
        config=CrawlConfig(page_limit=page_limit), now=clock or Clock(),
        client_factory=_fake_client_factory(pages), refresh_assessment=True,
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
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,error,"
        "legacy_run_id,evidence_count,observation_count"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, entity, "src_official_web", "sara.website",
         COLLECTOR_VERSION, config, config_hash, "complete",
         started_at, started_at, None, None, len(pages), 0))
    for index, (final_url, content, channels) in enumerate(pages):
        content_sha = wsha(content)
        evidence_id = wopaque("ev", session_id, final_url, content_sha)
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
                    "observation_ids": [],
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
    }
    assert set(journey) == allowed_doc_keys
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
    """CJ-02: a perfectly-shaped evidence row appended beneath a
    completed website session breaks the stored-vs-actual count contract;
    the session is invalid and NONE of its evidence is used."""
    from sara.website.model import canonical_json as wcanonical
    from sara.website.model import opaque_id as wopaque, sha256_text as wsha

    conn = prepared(tmp_path / "forged.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = acquire(conn, tmp_path, pages=BOOKING_SITE)
    session_id = stats.session_id
    stored_counts = conn.execute(
        "SELECT evidence_count, observation_count FROM acquisition_sessions "
        "WHERE id=?", (session_id,)).fetchone()
    # Forge: producer-perfect metadata, correct deterministic id, correct
    # entity binding, locator==final_url. ONLY the count drift betrays it.
    forged_url = "https://seed.example/forged"
    forged_content = "forged page body"
    forged_sha = wsha(forged_content)
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
        })
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_locator,source_role,status,retrieved_at,"
        "published_at,language,media_type,content_sha256,artifact_ref,metadata_json,created_at"
        ") VALUES (?,?,?,'https://seed.example/forged','official','usable',"
        "?,NULL,NULL,'text/html',?,NULL,?,?)",
        (forged_id, session_id, "src_official_web",
         "2026-09-26T12:00:09+00:00", forged_sha, forged_meta,
         "2026-09-26T12:00:09+00:00"))
    conn.commit()
    actual = conn.execute(
        "SELECT COUNT(*) FROM evidence_items WHERE acquisition_session_id=?",
        (session_id,)).fetchone()[0]
    assert actual == stored_counts[0] + 1
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at="2026-09-26T12:30:00+00:00")
    issues = dossier["integrity_issues"]
    session_issue = next(
        (i for i in issues
         if i["code"] == "customer_journey_website_session_invalid"), None)
    assert session_issue is not None
    assert session_issue["session_id"] == session_id
    journey = dossier["customer_journey"]
    used = {
        entry["evidence_id"]
        for stage in journey["stages"]
        for entry in stage["evidence"]
    }
    assert forged_id not in used
    # The manufacturing vector is closed: no channel or hand-off in the
    # document may originate from the invalidated session's metadata —
    # in particular the forged booking endpoint never appears.
    all_channel_ids = {
        (channel["identifier"], channel["normalized_identifier"])
        for stage in journey["stages"]
        for channel in stage["channels"]
    }
    assert ("https://booksy.com/forged", "https://booksy.com/forged") not in (
        all_channel_ids
    )
    # Fact provenance legitimately survives: booking/ordering capability
    # and transaction-type facts were reconciled from the REAL crawl's
    # observations, which the appended row cannot add or alter. The stage
    # therefore remains observed through fact evidence.
    book_order = next(
        s for s in journey["stages"] if s["stage"] == "book_order")
    assert book_order["evidence_state"] == "observed"
    assert book_order["evidence"]
    for handoff in journey["handoffs"]:
        assert handoff["evidence_id"] != forged_id
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
    """CJ-04: with a fresh and a stale crawl of the same site, an observed
    stage exposes only the current crawl's channels and evidence."""
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
    stale_rows = first_rows
    fresh_rows = all_rows - first_rows
    assert fresh_rows
    evaluated = "2026-11-10T12:00:00+00:00"  # 5 days after the second crawl
    dossier = build_business_dossier(
        conn, entity_id=entity, evaluated_at=evaluated)
    journey = dossier["customer_journey"]
    observed = [
        s for s in journey["stages"] if s["evidence_state"] == "observed"
    ]
    assert observed
    for stage in observed:
        used = {entry["evidence_id"] for entry in stage["evidence"]}
        assert used <= fresh_rows, stage["stage"]
        for channel in stage["channels"]:
            assert channel["evidence_id"] in fresh_rows
        for handoff in stage["handoffs"]:
            assert handoff["evidence_id"] in fresh_rows
    # Website-side hand-offs must cite the current crawl's rows. The
    # Maps->website hand-off cites MAPS evidence, whose currency follows
    # the official-website fact's own freshness window — assert it
    # exists (the fact is fresh) without forcing it into fresh_rows.
    for handoff in journey["handoffs"]:
        if handoff["from"] == "official_website":
            assert handoff["evidence_id"] in fresh_rows
    assert any(
        handoff["from"] == "maps_listing"
        for handoff in journey["handoffs"]
    )
    assert stale_rows
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
