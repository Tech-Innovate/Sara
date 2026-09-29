from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from unittest.mock import patch

from sara.maps_backfill import backfill_maps_business_understanding, business_entity_id_for_maps_business
from sara.migrations import apply_migrations
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
            requested_url=url,
            final_url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=self.pages[url].encode(),
            media_type="text/html",
            charset="utf-8",
        )


def factory(client: FakeClient):
    return lambda **_kwargs: client


PAGES = {
    "https://seed.example/": """
        <link rel="canonical" href="https://seed.example/">
        <a href="/book">Book appointment</a>
        <a href="/contact">Contact</a>
        <a href="https://wa.me/966501234567">WhatsApp</a>
    """,
    "https://seed.example/book": "<h1>Book</h1>",
    "https://seed.example/contact": """
        <a href="mailto:hello@seed.example">Email</a>
    """,
}


def prepared(path: Path) -> connect:
    conn = connect(path)
    assert apply_migrations(conn) == (1, 2)
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
    with patch("sara.storage.utc_now", return_value="2026-09-26T09:59:00+00:00"), patch(
        "sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"
    ):
        ingest_records(
            conn,
            "r1",
            [{
                "place_id": "place-seed", "cid": "cid-seed", "data_id": "data-seed",
                "title": "Business seed", "category": "Restaurant", "address": "Seed Street",
                "latitude": 21.55, "longitude": 39.18, "phone": "+966500000000",
                "website": "https://seed.example", "review_rating": 4.4, "review_count": 120,
                "status": "Open", "link": "https://maps.example/seed",
            }],
            finalize_run=("complete", 0, None),
        )
    with patch("sara.maps_backfill._utc_now", return_value="2026-09-26T10:00:00+00:00"):
        backfill_maps_business_understanding(conn)
    return conn


def absence_facts(conn, entity_id: str) -> list[tuple]:
    return list(conn.execute(
        "SELECT id, status, valid_to FROM facts WHERE subject_id=? "
        "AND status='not_observed' AND valid_to IS NULL",
        (entity_id,),
    ))


def test_exhausted_crawl_claims_absence(tmp_path: Path) -> None:
    """A crawl that exhausts its frontier may claim capability absence."""
    conn = prepared(tmp_path / "exhausted.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "ev",
        business_id=1,
        config=CrawlConfig(page_limit=8),
        now=Clock(),
        client_factory=factory(FakeClient(PAGES)),
    )
    assert stats.status == "complete"
    assert stats.crawl_frontier_exhausted is True
    assert stats.absence_claimable is True
    assert stats.not_observed_facts_created >= 1
    # whatsapp observed on the home page; online ordering appears nowhere, so
    # an exhausted full-surface inspection legitimately supports its absence.
    absent_predicates = {
        row[0] for row in conn.execute(
            "SELECT predicate FROM facts WHERE subject_id=? AND status='not_observed' "
            "AND valid_to IS NULL", (entity,))
    }
    assert "capability.online_ordering" in absent_predicates
    conn.close()


def test_budget_truncated_crawl_leaves_absence_unknown(tmp_path: Path) -> None:
    """Stopping at the page budget must not claim absence for unreached surfaces."""
    conn = prepared(tmp_path / "truncated.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "ev",
        business_id=1,
        config=CrawlConfig(page_limit=1),
        now=Clock(),
        client_factory=factory(FakeClient(PAGES)),
    )
    assert stats.status == "complete"
    assert stats.pages_fetched == 1
    assert stats.crawl_frontier_exhausted is False
    assert stats.absence_claimable is False
    assert stats.not_observed_facts_created == 0
    assert absence_facts(conn, entity) == []
    # Unobserved capabilities stay unknown, not absent.
    assert "capability.online_ordering" in stats.unresolved_predicates
    # Positive observations from the inspected page are still legitimate.
    assert stats.observations_created >= 1
    conn.close()


def test_prior_absence_survives_later_truncated_crawl(tmp_path: Path) -> None:
    """A later budget-truncated run must not erase an established absence."""
    conn = prepared(tmp_path / "survive.sqlite")
    entity = business_entity_id_for_maps_business(1)
    clock = Clock()
    first = collect_official_website(
        conn,
        evidence_root=tmp_path / "ev",
        business_id=1,
        config=CrawlConfig(page_limit=8),
        now=clock,
        client_factory=factory(FakeClient(PAGES)),
    )
    assert first.absence_claimable is True
    before = absence_facts(conn, entity)
    assert before

    second = collect_official_website(
        conn,
        evidence_root=tmp_path / "ev2",
        business_id=1,
        config=CrawlConfig(page_limit=1),
        now=clock,
        client_factory=factory(FakeClient(PAGES)),
    )
    assert second.absence_claimable is False
    assert second.not_observed_facts_created == 0
    after = absence_facts(conn, entity)
    assert [row[0] for row in after] == [row[0] for row in before]
    conn.close()


def test_assessment_refresh_wires_into_acquisition(tmp_path: Path) -> None:
    """refresh_assessment=True persists a sealed assessment for the entity."""
    conn = prepared(tmp_path / "refresh.sqlite")
    entity = business_entity_id_for_maps_business(1)
    clock = Clock()
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "ev",
        business_id=1,
        config=CrawlConfig(page_limit=8),
        now=clock,
        client_factory=factory(FakeClient(PAGES)),
        refresh_assessment=True,
    )
    assert stats.assessment_id is not None
    assert stats.assessment_already_assessed is False
    row = conn.execute(
        "SELECT business_entity_id, analysis_ready FROM dossier_assessments WHERE id=?",
        (stats.assessment_id,),
    ).fetchone()
    assert row[0] == entity
    assert bool(row[1]) is stats.assessment_analysis_ready
    seal = conn.execute(
        "SELECT sealed_at FROM dossier_assessment_seals WHERE assessment_id=?",
        (stats.assessment_id,),
    ).fetchone()
    assert seal is not None

    # A second acquisition with later evidence timestamps produces a new,
    # strictly later snapshot under the same entity.
    second = collect_official_website(
        conn,
        evidence_root=tmp_path / "ev2",
        business_id=1,
        config=CrawlConfig(page_limit=8),
        now=clock,
        client_factory=factory(FakeClient(PAGES)),
        refresh_assessment=True,
    )
    assert second.assessment_id is not None
    assert second.assessment_id != stats.assessment_id
    clocks = [r[0] for r in conn.execute(
        "SELECT computed_at FROM dossier_assessments WHERE business_entity_id=?", (entity,)
    )]
    assert len(clocks) == 2
    conn.close()


def test_no_refresh_leaves_no_assessment(tmp_path: Path) -> None:
    """Default programmatic behavior does not persist assessments."""
    conn = prepared(tmp_path / "norefresh.sqlite")
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "ev",
        business_id=1,
        config=CrawlConfig(page_limit=8),
        now=Clock(),
        client_factory=factory(FakeClient(PAGES)),
    )
    assert stats.assessment_id is None
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 0
    conn.close()


def test_blocked_acquisition_never_reaches_assessment(tmp_path: Path) -> None:
    """A blocked crawl creates no usable session and no assessment."""

    class BlockedClient:
        def fetch(self, url: str) -> HttpResponse:
            raise WebsiteBlockedError("robots policy disallows " + url)

    conn = prepared(tmp_path / "blocked.sqlite")
    with pytest.raises(WebsiteAcquisitionError):
        collect_official_website(
            conn,
            evidence_root=tmp_path / "ev",
            business_id=1,
            config=CrawlConfig(page_limit=8),
            now=Clock(),
            client_factory=factory(BlockedClient()),
            refresh_assessment=True,
        )
    assert conn.execute("SELECT COUNT(*) FROM dossier_assessments").fetchone()[0] == 0
    conn.close()



DEPTH_PAGES = {
    "https://seed.example/": """
        <link rel="canonical" href="https://seed.example/">
        <a href="/about">About</a>
    """,
    "https://seed.example/about": """
        <a href="/booking">Booking</a>
    """,
    "https://seed.example/booking": "<h1>Booking</h1>",
}


def test_depth_truncated_crawl_leaves_absence_unknown(tmp_path: Path) -> None:
    """Stopping at the depth budget must not claim absence for deeper surfaces."""
    conn = prepared(tmp_path / "depth-truncated.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "ev",
        business_id=1,
        config=CrawlConfig(page_limit=8, depth_limit=1),
        now=Clock(),
        client_factory=factory(FakeClient(DEPTH_PAGES)),
    )
    assert stats.status == "complete"
    # home (depth 0) and about (depth 1) fetched; /booking deliberately
    # unvisited, so the frontier was cut by the depth budget.
    assert stats.pages_fetched == 2
    assert stats.crawl_frontier_exhausted is False
    assert stats.crawl_depth_truncated is True
    assert stats.absence_claimable is False
    assert stats.not_observed_facts_created == 0
    assert absence_facts(conn, entity) == []
    conn.close()


def test_depth_sufficient_crawl_exhausts_and_claims_absence(tmp_path: Path) -> None:
    """The same site with sufficient depth genuinely exhausts the frontier."""
    conn = prepared(tmp_path / "depth-ok.sqlite")
    entity = business_entity_id_for_maps_business(1)
    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "ev",
        business_id=1,
        config=CrawlConfig(page_limit=8, depth_limit=2),
        now=Clock(),
        client_factory=factory(FakeClient(DEPTH_PAGES)),
    )
    assert stats.status == "complete"
    assert stats.pages_fetched == 3
    assert stats.crawl_frontier_exhausted is True
    assert stats.crawl_depth_truncated is False
    assert stats.absence_claimable is True
    assert stats.not_observed_facts_created >= 1
    assert absence_facts(conn, entity)
    conn.close()


def test_assessment_uses_the_injected_clock(tmp_path: Path) -> None:
    """A deterministic future clock must flow into the assessment, not wall time."""
    conn = prepared(tmp_path / "future-clock.sqlite")

    class FutureClock:
        def __init__(self) -> None:
            from datetime import datetime, timedelta, timezone

            self.current = datetime(2030, 1, 1, tzinfo=timezone.utc)

        def __call__(self) -> str:
            value = self.current.isoformat()
            self.current += timedelta(seconds=1)
            return value

    stats = collect_official_website(
        conn,
        evidence_root=tmp_path / "ev",
        business_id=1,
        config=CrawlConfig(page_limit=8),
        now=FutureClock(),
        client_factory=factory(FakeClient(PAGES)),
        refresh_assessment=True,
    )
    assert stats.assessment_id is not None
    computed_at = conn.execute(
        "SELECT computed_at FROM dossier_assessments WHERE id=?",
        (stats.assessment_id,),
    ).fetchone()[0]
    # With the old wall-clock assessment, 2030 evidence timestamps would
    # have made the watermark exceed the computed time and failed closed.
    assert computed_at.startswith("2030-01-01")
    conn.close()


def test_cli_reports_assessment_failure_as_controlled_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A post-commit assessment failure exits 2 with a message, not a traceback."""
    import sara.website.surface as surface_module
    from sara.dossier.assessment import DossierAssessmentError
    from sara.website.surface import main

    conn = prepared(tmp_path / "cli-failure.sqlite")
    db_path = str(tmp_path / "cli-failure.sqlite")
    conn.close()

    real_collect = surface_module.collect_official_website

    class FailingStats:
        assessment_id = None

    def collect_with_failing_assessment(*_args, **kwargs):
        # Simulate: acquisition commits, then the assessment refresh raises.
        raise DossierAssessmentError("dossier assessment failed: boom")

    monkeypatch.setattr(
        surface_module, "collect_official_website", collect_with_failing_assessment
    )
    rc = main([
        "--db", db_path,
        "--evidence-dir", str(tmp_path / "ev"),
        "--business-id", "1",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "dossier assessment failed: boom" in err
    assert "Traceback" not in err
