from pathlib import Path

from sara.website.crawl import crawl_official_site
from sara.website.http import HttpResponse
from sara.website.model import CrawlConfig


class FakeClient:
    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self.responses = responses

    def fetch(self, url: str) -> HttpResponse:
        return self.responses[url]


def response(requested: str, final: str, body: str, *, charset: str = "utf-8") -> HttpResponse:
    return HttpResponse(
        requested_url=requested,
        final_url=final,
        status=200,
        headers={"content-type": f"text/html; charset={charset}"},
        body=body.encode("utf-8"),
        media_type="text/html",
        charset=charset,
    )


def test_redirect_alias_to_home_is_captured_once(tmp_path: Path) -> None:
    start = "https://example.com/start"
    root = "https://example.com/"
    client = FakeClient(
        {
            start: response(start, root, '<link rel="canonical" href="https://example.com/">'),
        }
    )
    result = crawl_official_site(
        entity_id="be",
        session_id="acq",
        start_url=start,
        evidence_root=tmp_path,
        config=CrawlConfig(page_limit=2, depth_limit=0),
        client=client,
        now=lambda: "2026-09-26T07:00:00+00:00",
    )
    assert len(result.captures) == 1
    assert result.captures[0].final_url == root
    assert result.canonical_home_url == root
    assert result.errors == ()
    assert len(list(tmp_path.rglob("*.html"))) == 1


def test_unknown_declared_charset_falls_back_without_losing_page(tmp_path: Path) -> None:
    root = "https://example.com/"
    client = FakeClient(
        {root: response(root, root, "<title>Business</title>", charset="x-not-a-codec")}
    )
    result = crawl_official_site(
        entity_id="be",
        session_id="acq",
        start_url=root,
        evidence_root=tmp_path,
        config=CrawlConfig(page_limit=1, depth_limit=0),
        client=client,
        now=lambda: "2026-09-26T07:00:00+00:00",
    )
    assert len(result.captures) == 1
    assert result.captures[0].parsed.title == "Business"
    assert result.errors == ()
    assert len(list(tmp_path.rglob("*.html"))) == 1


def test_parser_failure_retains_raw_artifact_and_marks_page_error(tmp_path: Path, monkeypatch) -> None:
    root = "https://example.com/"
    client = FakeClient({root: response(root, root, "raw evidence")})

    def fail_parse(_url: str, _text: str):
        raise ValueError("synthetic parse failure")

    monkeypatch.setattr("sara.website.crawl.parse_html", fail_parse)
    result = crawl_official_site(
        entity_id="be",
        session_id="acq",
        start_url=root,
        evidence_root=tmp_path,
        config=CrawlConfig(page_limit=1, depth_limit=0),
        client=client,
        now=lambda: "2026-09-26T07:00:00+00:00",
    )
    assert result.captures == ()
    assert len(result.errors) == 1
    assert "synthetic parse failure" in result.errors[0]
    assert len(list(tmp_path.rglob("*.html"))) == 1
