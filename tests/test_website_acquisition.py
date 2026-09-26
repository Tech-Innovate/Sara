from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sara.dossier import build_business_dossier
from sara.maps_backfill import backfill_maps_business_understanding, business_entity_id_for_maps_business
from sara.migrations import apply_migrations
from sara.storage import connect, connect_existing, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary
from sara.website import CrawlConfig, WebsiteAcquisitionError, collect_official_website
from sara.website.http import HttpResponse, WebsiteFetchError


class Clock:
    def __init__(self, start: str = "2026-09-26T06:00:00+00:00") -> None:
        self.current = datetime.fromisoformat(start)

    def __call__(self) -> str:
        result = self.current.isoformat()
        self.current += timedelta(seconds=1)
        return result


class FakeClient:
    def __init__(self, pages: dict[str, str], failures: set[str] | None = None) -> None:
        self.pages = pages
        self.failures = failures or set()
        self.requested: list[str] = []

    def fetch(self, url: str) -> HttpResponse:
        self.requested.append(url)
        if url in self.failures or url not in self.pages:
            raise WebsiteFetchError(f"HTTP 404 for {url}")
        body = self.pages[url].encode("utf-8")
        return HttpResponse(
            requested_url=url,
            final_url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=body,
            media_type="text/html",
            charset="utf-8",
        )


def _factory(client: FakeClient):
    def create(**_kwargs):
        return client

    return create


def _prepared(path: Path):
    conn = connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,"
        "raw_path,status,started_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "r1",
            "test-area",
            '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            2.0,
            1,
            '["restaurant"]',
            "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}',
            "/evidence/r1.jsonl",
            "running",
            "2026-09-25T10:00:00+00:00",
        ),
    )
    conn.commit()
    ingest_records(
        conn,
        "r1",
        [
            {
                "place_id": "place-seed",
                "cid": "cid-seed",
                "data_id": "data-seed",
                "title": "Business seed",
                "category": "Restaurant",
                "address": "Seed Street",
                "latitude": 21.55,
                "longitude": 39.18,
                "phone": "+966500000000",
                "website": "https://seed.example",
                "review_rating": 4.4,
                "review_count": 120,
                "status": "Open",
                "link": "https://maps.example/seed",
            }
        ],
        finalize_run=("complete", 0, None),
    )
    backfill_maps_business_understanding(conn)
    return conn


def _fact(conn, entity_id: str, predicate: str):
    return conn.execute(
        "SELECT id,value_json,status,valid_from,last_verified_at FROM facts "
        "WHERE subject_id=? AND predicate=? AND valid_to IS NULL ORDER BY id",
        (entity_id, predicate),
    ).fetchone()


def test_complete_acquisition_retains_evidence_channels_and_fact_history(tmp_path: Path) -> None:
    db = tmp_path / "website.sqlite"
    conn = _prepared(db)
    entity_id = business_entity_id_for_maps_business(1)
    original_website_fact = _fact(conn, entity_id, "business.website.official")[0]

    client = FakeClient(
        {
            "https://seed.example/": """
                <html><head><title>Seed</title><link rel="canonical" href="https://seed.example/"></head>
                <body>
                  <a href="/book">Book appointment</a>
                  <a href="/contact">Contact</a>
                  <a href="https://wa.me/966501234567">WhatsApp</a>
                  <a href="https://instagram.com/seed">Instagram</a>
                </body></html>
            """,
            "https://seed.example/book": "<html><body><h1>Book</h1></body></html>",
            "https://seed.example/contact": """
                <html><body>
                  <a href="mailto:hello@seed.example">Email</a>
                  <a href="tel:+966501111111">Call</a>
                </body></html>
            """,
        }
    )
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "evidence",
        business_id=1,
        config=CrawlConfig(page_limit=3, depth_limit=1),
        now=Clock(),
        client_factory=_factory(client),
    )

    assert stats.status == "complete"
    assert stats.pages_fetched == 3
    assert stats.evidence_items_created == 3
    assert stats.not_observed_facts_created == 1
    assert stats.canonical_home_url == "https://seed.example/"
    assert len(list((tmp_path / "evidence").rglob("*.html"))) == 3

    session = conn.execute(
        "SELECT source_id,status,evidence_count,observation_count,error "
        "FROM acquisition_sessions WHERE id=?",
        (stats.session_id,),
    ).fetchone()
    assert tuple(session[:4]) == (
        "src_official_web",
        "complete",
        stats.evidence_items_created,
        stats.observations_created,
    )
    assert session[4] is None

    current_website = _fact(conn, entity_id, "business.website.official")
    assert current_website[0] != original_website_fact
    assert json.loads(current_website[1]) == "https://seed.example/"
    assert current_website[2] == "single_source"
    assert conn.execute(
        "SELECT valid_to FROM facts WHERE id=?", (original_website_fact,)
    ).fetchone()[0] is not None

    assert json.loads(_fact(conn, entity_id, "capability.online_booking")[1]) is True
    assert json.loads(_fact(conn, entity_id, "capability.whatsapp")[1]) is True
    ordering = _fact(conn, entity_id, "capability.online_ordering")
    assert ordering[1] is None
    assert ordering[2] == "not_observed"
    assert conn.execute(
        "SELECT support_role FROM fact_acquisition_support WHERE fact_id=?",
        (ordering[0],),
    ).fetchone()[0] == "supports_absence"

    channel_types = {
        row[0]
        for row in conn.execute(
            "SELECT channel_type FROM channels WHERE business_entity_id=?",
            (entity_id,),
        )
    }
    assert {"website", "booking", "whatsapp", "instagram", "email", "phone"} <= channel_types

    dossier = build_business_dossier(
        conn,
        entity_id=entity_id,
        evaluated_at="2026-09-26T07:00:00+00:00",
    )
    assert dossier["integrity_issues"] == []
    current = {fact["predicate"]: fact for fact in dossier["facts"] if fact["fact_slot"] == "__single__"}
    assert current["capability.online_booking"]["value"] is True
    assert current["capability.online_ordering"]["semantic_state"] == "not_observed"
    conn.close()


def test_complete_bounded_acquisition_records_not_observed_without_claiming_false(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "absence.sqlite")
    entity_id = business_entity_id_for_maps_business(1)
    client = FakeClient(
        {
            "https://seed.example/": "<html><head><title>Seed</title></head><body>Plain site</body></html>"
        }
    )
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "evidence",
        entity_id=entity_id,
        config=CrawlConfig(page_limit=1, depth_limit=0),
        now=Clock(),
        client_factory=_factory(client),
    )
    assert stats.status == "complete"
    assert stats.not_observed_facts_created == 3
    for predicate in (
        "capability.online_booking",
        "capability.online_ordering",
        "capability.whatsapp",
    ):
        fact = _fact(conn, entity_id, predicate)
        assert fact[1] is None
        assert fact[2] == "not_observed"
        assert conn.execute(
            "SELECT support_role FROM fact_acquisition_support WHERE fact_id=?",
            (fact[0],),
        ).fetchone()[0] == "supports_absence"
    conn.close()


def test_partial_acquisition_never_manufactures_not_observed(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "partial.sqlite")
    entity_id = business_entity_id_for_maps_business(1)
    client = FakeClient(
        {
            "https://seed.example/": '<html><body><a href="/services">Services</a></body></html>'
        },
        failures={"https://seed.example/services"},
    )
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "evidence",
        entity_id=entity_id,
        config=CrawlConfig(page_limit=2, depth_limit=1),
        now=Clock(),
        client_factory=_factory(client),
    )
    assert stats.status == "partial"
    assert stats.not_observed_facts_created == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM facts WHERE subject_id=? AND status='not_observed' AND valid_to IS NULL",
        (entity_id,),
    ).fetchone()[0] == 0
    conn.close()


def test_location_page_phone_is_evidence_but_not_promoted_business_wide(tmp_path: Path) -> None:
    conn = _prepared(tmp_path / "scope.sqlite")
    entity_id = business_entity_id_for_maps_business(1)
    client = FakeClient(
        {
            "https://seed.example/": '<html><body><a href="/locations">Locations</a></body></html>',
            "https://seed.example/locations": '<html><body><a href="tel:+966502222222">Branch phone</a></body></html>',
        }
    )
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "evidence",
        entity_id=entity_id,
        config=CrawlConfig(page_limit=2, depth_limit=1),
        now=Clock(),
        client_factory=_factory(client),
    )
    assert stats.status == "complete"
    assert conn.execute(
        "SELECT COUNT(*) FROM channels WHERE business_entity_id=? AND channel_type='phone'",
        (entity_id,),
    ).fetchone()[0] == 0
    metadata = [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT metadata_json FROM evidence_items WHERE acquisition_session_id=?",
            (stats.session_id,),
        )
    ]
    branch_channel = [
        channel
        for item in metadata
        for channel in item["channels"]
        if channel["channel_type"] == "phone"
    ][0]
    assert branch_channel["canonicalized_channel_id"] is None
    assert branch_channel["canonicalized_scope"] is None
    conn.close()


def test_ingestion_failure_keeps_raw_artifact_and_marks_session_failed(tmp_path: Path, monkeypatch) -> None:
    conn = _prepared(tmp_path / "failure.sqlite")
    entity_id = business_entity_id_for_maps_business(1)
    client = FakeClient(
        {"https://seed.example/": "<html><body>Evidence before DB failure</body></html>"}
    )

    import sara.website.persistence as persistence

    def explode(*_args, **_kwargs):
        raise WebsiteAcquisitionError("synthetic persistence failure")

    monkeypatch.setattr(persistence, "_ensure_channel", explode)
    with pytest.raises(WebsiteAcquisitionError, match="synthetic persistence failure"):
        collect_official_website(
            conn,
            evidence_root=tmp_path / "evidence",
            entity_id=entity_id,
            config=CrawlConfig(page_limit=1, depth_limit=0),
            now=Clock(),
            client_factory=_factory(client),
        )

    artifacts = list((tmp_path / "evidence").rglob("*.html"))
    assert len(artifacts) == 1
    session = conn.execute(
        "SELECT id,status,error FROM acquisition_sessions WHERE source_id='src_official_web'"
    ).fetchone()
    assert session[1] == "failed"
    assert "synthetic persistence failure" in session[2]
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_items WHERE acquisition_session_id=?", (session[0],)
    ).fetchone()[0] == 0
    conn.close()


def test_unsynchronized_business_fails_before_network_or_session_creation(tmp_path: Path) -> None:
    db = tmp_path / "unsynchronized.sqlite"
    conn = connect(db)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    called = False

    def factory(**_kwargs):
        nonlocal called
        called = True
        return FakeClient({})

    with pytest.raises(WebsiteAcquisitionError):
        collect_official_website(
            conn,
            evidence_root=tmp_path / "evidence",
            business_id=1,
            now=Clock(),
            client_factory=factory,
        )
    assert called is False
    assert conn.execute(
        "SELECT COUNT(*) FROM acquisition_sessions WHERE source_id='src_official_web'"
    ).fetchone()[0] == 0
    conn.close()
