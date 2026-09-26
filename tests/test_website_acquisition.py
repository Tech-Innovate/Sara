from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sara.dossier import build_business_dossier
from sara.maps_backfill import backfill_maps_business_understanding, business_entity_id_for_maps_business
from sara.migrations import apply_migrations
from sara.storage import connect, ingest_records
from sara.understanding_vocabulary import seed_business_understanding_vocabulary
from sara.website import CrawlConfig, WebsiteAcquisitionError, collect_official_website
from sara.website.http import HttpResponse, WebsiteFetchError


class Clock:
    def __init__(self) -> None:
        self.current = datetime.fromisoformat("2026-09-26T06:00:00+00:00")

    def __call__(self) -> str:
        value = self.current.isoformat()
        self.current += timedelta(seconds=1)
        return value


class SequenceClock:
    def __init__(self, values: list[str]) -> None:
        self.values = iter(values)

    def __call__(self) -> str:
        return next(self.values)


class FakeClient:
    def __init__(self, pages: dict[str, str], failures: set[str] | None = None) -> None:
        self.pages = pages
        self.failures = failures or set()

    def fetch(self, url: str) -> HttpResponse:
        if url in self.failures or url not in self.pages:
            raise WebsiteFetchError(f"HTTP 404 for {url}")
        body = self.pages[url].encode()
        return HttpResponse(
            requested_url=url,
            final_url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=body,
            media_type="text/html",
            charset="utf-8",
        )


def factory(client: FakeClient):
    return lambda **_kwargs: client


def prepared(path: Path, *, website: str = "https://seed.example"):
    conn = connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO runs(id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,"
        "config_json,raw_path,status,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "r1", "test", '{"max_lat":22,"max_lon":40,"min_lat":21,"min_lon":39}',
            2.0, 1, '["restaurant"]', "gosom/google-maps-scraper:v1.18.1",
            '{"strict_bounds":true}', "/evidence/r1.jsonl", "running",
            "2026-09-25T10:00:00+00:00",
        ),
    )
    conn.commit()
    ingest_records(
        conn,
        "r1",
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


def fact(conn, entity_id: str, predicate: str):
    return conn.execute(
        "SELECT id,value_json,status FROM facts WHERE subject_id=? AND predicate=? "
        "AND valid_to IS NULL ORDER BY id",
        (entity_id, predicate),
    ).fetchone()


def test_complete_acquisition_is_dossier_clean_and_preserves_absence_semantics(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "website.sqlite")
    entity_id = business_entity_id_for_maps_business(1)
    original_website_fact = fact(conn, entity_id, "business.website.official")[0]
    client = FakeClient({
        "https://seed.example/": """
            <link rel="canonical" href="https://seed.example/">
            <a href="/book">Book appointment</a>
            <a href="/contact">Contact</a>
            <a href="https://wa.me/966501234567">WhatsApp</a>
            <a href="https://instagram.com/seed">Instagram</a>
        """,
        "https://seed.example/book": "<h1>Book</h1>",
        "https://seed.example/contact": """
            <a href="mailto:hello@seed.example">Email</a>
            <a href="tel:+966501111111">Call</a>
        """,
    })
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "evidence",
        business_id=1,
        config=CrawlConfig(page_limit=3, depth_limit=1),
        now=Clock(),
        client_factory=factory(client),
    )
    assert stats.status == "complete"
    assert stats.pages_fetched == 3
    assert len(list((tmp_path / "evidence").rglob("*.html"))) == 3
    assert fact(conn, entity_id, "business.website.official")[0] != original_website_fact
    assert json.loads(fact(conn, entity_id, "capability.online_booking")[1]) is True
    assert json.loads(fact(conn, entity_id, "capability.whatsapp")[1]) is True
    ordering = fact(conn, entity_id, "capability.online_ordering")
    assert ordering[1] is None and ordering[2] == "not_observed"
    assert conn.execute(
        "SELECT support_role FROM fact_acquisition_support WHERE fact_id=?", (ordering[0],)
    ).fetchone()[0] == "supports_absence"
    channel_types = {row[0] for row in conn.execute(
        "SELECT channel_type FROM channels WHERE business_entity_id=?", (entity_id,)
    )}
    assert {"website", "booking", "whatsapp", "instagram"} <= channel_types
    assert not ({"email", "phone"} & channel_types)
    metadata = [json.loads(row[0]) for row in conn.execute(
        "SELECT metadata_json FROM evidence_items WHERE acquisition_session_id=?", (stats.session_id,)
    )]
    contact_channels = [
        channel
        for item in metadata
        if item["page_role"] == "contact"
        for channel in item["channels"]
    ]
    assert {item["channel_type"] for item in contact_channels} == {"email", "phone"}
    assert all(item["canonicalized_channel_id"] is None for item in contact_channels)

    dossier = build_business_dossier(
        conn, entity_id=entity_id, evaluated_at="2026-09-26T07:00:00+00:00"
    )
    assert dossier["integrity_issues"] == []
    current = {
        item["predicate"]: item
        for item in dossier["facts"]
        if item["fact_slot"] == "__single__"
    }
    assert current["capability.online_booking"]["value"] is True
    assert current["capability.online_ordering"]["status"] == "not_observed"
    conn.close()


def test_partial_acquisition_never_creates_not_observed(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "partial.sqlite")
    entity_id = business_entity_id_for_maps_business(1)
    client = FakeClient(
        {"https://seed.example/": '<a href="/services">Services</a>'},
        failures={"https://seed.example/services"},
    )
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "evidence",
        entity_id=entity_id,
        config=CrawlConfig(page_limit=2, depth_limit=1),
        now=Clock(),
        client_factory=factory(client),
    )
    assert stats.status == "partial"
    assert stats.not_observed_facts_created == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM facts WHERE subject_id=? AND status='not_observed' AND valid_to IS NULL",
        (entity_id,),
    ).fetchone()[0] == 0
    conn.close()


def test_branch_page_channel_is_retained_as_evidence_without_entity_scope_promotion(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "scope.sqlite")
    entity_id = business_entity_id_for_maps_business(1)
    client = FakeClient({
        "https://seed.example/": '<a href="/locations">Locations</a>',
        "https://seed.example/locations": '<a href="tel:+966502222222">Branch phone</a>',
    })
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "evidence",
        entity_id=entity_id,
        config=CrawlConfig(page_limit=2, depth_limit=1),
        now=Clock(),
        client_factory=factory(client),
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM channels WHERE business_entity_id=? AND channel_type='phone'",
        (entity_id,),
    ).fetchone()[0] == 0
    metadata = [json.loads(row[0]) for row in conn.execute(
        "SELECT metadata_json FROM evidence_items WHERE acquisition_session_id=?", (stats.session_id,)
    )]
    phone = [c for item in metadata for c in item["channels"] if c["channel_type"] == "phone"][0]
    assert phone["canonicalized_channel_id"] is None
    conn.close()


def test_deep_start_uses_fetched_root_as_home_and_does_not_promote_branch_contact(tmp_path: Path) -> None:
    conn = prepared(
        tmp_path / "deep.sqlite",
        website="https://seed.example/location/jeddah",
    )
    entity_id = business_entity_id_for_maps_business(1)
    client = FakeClient({
        "https://seed.example/location/jeddah": """
            <link rel="canonical" href="https://seed.example/location/jeddah">
            <a href="tel:+966503333333">Jeddah branch</a>
        """,
        "https://seed.example/": """
            <link rel="canonical" href="https://seed.example/">
            <a href="https://instagram.com/seed">Instagram</a>
        """,
    })
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "evidence",
        entity_id=entity_id,
        config=CrawlConfig(page_limit=2, depth_limit=0),
        now=Clock(),
        client_factory=factory(client),
    )
    assert stats.status == "complete"
    assert stats.canonical_home_url == "https://seed.example/"
    assert json.loads(fact(conn, entity_id, "business.website.official")[1]) == "https://seed.example/"
    assert conn.execute(
        "SELECT COUNT(*) FROM channels WHERE business_entity_id=? AND channel_type='phone'",
        (entity_id,),
    ).fetchone()[0] == 0
    metadata = [json.loads(row[0]) for row in conn.execute(
        "SELECT metadata_json FROM evidence_items WHERE acquisition_session_id=?", (stats.session_id,)
    )]
    deep = next(item for item in metadata if item["final_url"].endswith("/location/jeddah"))
    phone = next(item for item in deep["channels"] if item["channel_type"] == "phone")
    assert deep["home_page"] is False
    assert phone["canonicalized_channel_id"] is None
    conn.close()


def test_deep_start_without_home_capture_stays_partial_and_emits_no_absence(tmp_path: Path) -> None:
    conn = prepared(
        tmp_path / "deep-partial.sqlite",
        website="https://seed.example/location/jeddah",
    )
    entity_id = business_entity_id_for_maps_business(1)
    client = FakeClient({
        "https://seed.example/location/jeddah": """
            <link rel="canonical" href="https://seed.example/location/jeddah">
        """,
    })
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "evidence",
        entity_id=entity_id,
        config=CrawlConfig(page_limit=1, depth_limit=0),
        now=Clock(),
        client_factory=factory(client),
    )
    assert stats.status == "partial"
    assert stats.canonical_home_url is None
    assert stats.not_observed_facts_created == 0
    assert "capability.online_booking" in stats.unresolved_predicates
    conn.close()


def test_group_reconciliation_uses_actual_instants_across_offsets(tmp_path: Path) -> None:
    conn = prepared(tmp_path / "offset-group.sqlite")
    entity_id = business_entity_id_for_maps_business(1)
    client = FakeClient({
        "https://seed.example/": '<a href="/book">Book now</a>',
        "https://seed.example/book": '<a href="/reserve">Reserve appointment</a>',
    })
    clock = SequenceClock([
        "2026-09-26T06:00:00+00:00",
        "2026-09-26T09:30:00+03:00",
        "2026-09-26T07:00:00+00:00",
        "2026-09-26T07:00:01+00:00",
    ])
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "evidence",
        entity_id=entity_id,
        config=CrawlConfig(page_limit=2, depth_limit=1),
        now=clock,
        client_factory=factory(client),
    )
    assert stats.status == "complete"
    current = conn.execute(
        "SELECT last_verified_at FROM facts WHERE subject_id=? "
        "AND predicate='capability.online_booking' AND valid_to IS NULL",
        (entity_id,),
    ).fetchone()
    assert current[0] == "2026-09-26T07:00:00+00:00"
    conn.close()


def test_ingestion_failure_leaves_raw_artifact_but_no_registered_evidence(tmp_path: Path, monkeypatch) -> None:
    conn = prepared(tmp_path / "failure.sqlite")
    entity_id = business_entity_id_for_maps_business(1)
    client = FakeClient({"https://seed.example/": "Evidence before DB failure"})
    import sara.website.persistence as persistence

    monkeypatch.setattr(
        persistence,
        "_ensure_channel",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            WebsiteAcquisitionError("synthetic persistence failure")
        ),
    )
    with pytest.raises(WebsiteAcquisitionError, match="synthetic persistence failure"):
        collect_official_website(
            conn,
            evidence_root=tmp_path / "evidence",
            entity_id=entity_id,
            config=CrawlConfig(page_limit=1, depth_limit=0),
            now=Clock(),
            client_factory=factory(client),
        )
    assert len(list((tmp_path / "evidence").rglob("*.html"))) == 1
    session = conn.execute(
        "SELECT id,status,error FROM acquisition_sessions WHERE source_id='src_official_web'"
    ).fetchone()
    assert session[1] == "failed" and "synthetic persistence failure" in session[2]
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_items WHERE acquisition_session_id=?", (session[0],)
    ).fetchone()[0] == 0
    conn.close()


def test_unsynchronized_business_fails_before_network(tmp_path: Path) -> None:
    conn = connect(tmp_path / "unsynchronized.sqlite")
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    called = False

    def should_not_run(**_kwargs):
        nonlocal called
        called = True
        return FakeClient({})

    with pytest.raises(WebsiteAcquisitionError):
        collect_official_website(
            conn,
            evidence_root=tmp_path / "evidence",
            business_id=1,
            now=Clock(),
            client_factory=should_not_run,
        )
    assert called is False
    assert conn.execute(
        "SELECT COUNT(*) FROM acquisition_sessions WHERE source_id='src_official_web'"
    ).fetchone()[0] == 0
    conn.close()
