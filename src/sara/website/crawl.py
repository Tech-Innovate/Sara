from __future__ import annotations

import heapq
import os
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from .http import HttpResponse, SafeHttpClient, WebsiteBlockedError, WebsiteFetchError
from .model import CrawlConfig, CrawlResult, PageCapture, sha256_bytes, sha256_text
from .parser import normalize_http_url, parse_html, same_site


def page_role(url: str) -> str:
    path = urlsplit(url).path.lower()
    for role, terms in (
        ("booking", ("book", "booking", "reserve", "reservation", "appointment")),
        ("ordering", ("order", "ordering", "delivery", "pickup", "takeaway", "takeout")),
        ("contact", ("contact",)),
        ("offerings", ("service", "services", "product", "products", "menu", "pricing")),
        ("locations", ("location", "locations", "branches")),
        ("about", ("about",)),
        ("support", ("support", "help", "faq")),
        ("careers", ("career", "careers", "jobs")),
    ):
        if any(term in path for term in terms):
            return role
    return "home" if path in {"", "/"} else "other"


def origin_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    display_host = f"[{host}]" if ":" in host else host
    port = parsed.port
    if port and not (
        (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
    ):
        netloc = f"{display_host}:{port}"
    else:
        netloc = display_host
    return urlunsplit((parsed.scheme, netloc, "/", "", ""))


def _artifact_path(
    root: Path,
    *,
    entity_id: str,
    session_id: str,
    final_url: str,
    content_sha256: str,
) -> Path:
    url_hash = sha256_text(final_url)[:12]
    return root / entity_id / session_id / f"{content_sha256[:24]}-{url_hash}.html"


def _write_artifact(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _is_home_capture(capture: PageCapture, *, start_url: str) -> bool:
    root = origin_url(start_url)
    requested = normalize_http_url(capture.requested_url)
    final = normalize_http_url(capture.final_url)
    return requested == root or final == root


def _canonical_home(capture: PageCapture, *, start_url: str) -> str:
    final = normalize_http_url(capture.final_url)
    if final is None:
        return capture.final_url
    candidate = capture.parsed.canonical_url
    if candidate is None or not same_site(candidate, start_url):
        return final
    normalized_candidate = normalize_http_url(candidate)
    if normalized_candidate is None:
        return final
    candidate_parts = urlsplit(normalized_candidate)
    final_parts = urlsplit(final)
    if final_parts.scheme == "https" and candidate_parts.scheme != "https":
        return final
    if (candidate_parts.path, candidate_parts.query) != (
        final_parts.path,
        final_parts.query,
    ):
        return final
    return normalized_candidate


def _decode_page(response: HttpResponse) -> str:
    try:
        return response.body.decode(response.charset, errors="replace")
    except LookupError:
        return response.body.decode("utf-8", errors="replace")


def crawl_official_site(
    *,
    entity_id: str,
    session_id: str,
    start_url: str,
    evidence_root: Path,
    config: CrawlConfig,
    client: SafeHttpClient,
    now: Callable[[], str],
) -> CrawlResult:
    """Crawl one verified official site within an explicit page/depth budget.

    Raw page bytes are durably written before parsing. Failed or malformed pages
    are recorded as errors and do not discard already-fetched artifacts; the
    caller decides whether the acquisition is complete or partial.
    """
    queue: list[tuple[int, int, str]] = []
    heapq.heappush(queue, (-10_000, 0, start_url))
    root = origin_url(start_url)
    if root != start_url:
        heapq.heappush(queue, (-9_000, 0, root))

    seen: set[str] = set()
    captured_final_urls: set[str] = set()
    captures: list[PageCapture] = []
    errors: list[str] = []

    while queue and len(captures) < config.page_limit:
        _negative_priority, depth, raw_url = heapq.heappop(queue)
        url = normalize_http_url(raw_url)
        if url is None or url in seen or depth > config.depth_limit:
            continue
        seen.add(url)
        try:
            response: HttpResponse = client.fetch(url)
        except (WebsiteBlockedError, WebsiteFetchError) as exc:
            errors.append(f"{url}: {exc}")
            continue

        final_url = normalize_http_url(response.final_url)
        if final_url is None:
            errors.append(f"{url}: response returned an invalid final URL")
            continue
        seen.add(final_url)
        if final_url in captured_final_urls:
            continue

        content_hash = sha256_bytes(response.body)
        artifact = _artifact_path(
            evidence_root,
            entity_id=entity_id,
            session_id=session_id,
            final_url=final_url,
            content_sha256=content_hash,
        )
        _write_artifact(artifact, response.body)
        try:
            parsed = parse_html(final_url, _decode_page(response))
        except Exception as exc:  # HTML is untrusted input; retain bytes and fail this page closed.
            errors.append(f"{final_url}: HTML parsing failed: {exc}")
            continue

        retrieved_at = now()
        captured_final_urls.add(final_url)
        captures.append(
            PageCapture(
                requested_url=response.requested_url,
                final_url=final_url,
                depth=depth,
                retrieved_at=retrieved_at,
                status=response.status,
                media_type=response.media_type,
                charset=response.charset,
                headers=response.headers,
                body=response.body,
                content_sha256=content_hash,
                artifact_ref=str(artifact),
                parsed=parsed,
            )
        )

        if depth >= config.depth_limit:
            continue
        for link in parsed.links:
            if same_site(link.url, start_url) and link.url not in seen:
                heapq.heappush(queue, (-link.priority, depth + 1, link.url))

    home_capture = next(
        (
            capture
            for capture in captures
            if _is_home_capture(capture, start_url=start_url)
        ),
        None,
    )
    canonical_home = (
        None
        if home_capture is None
        else _canonical_home(home_capture, start_url=start_url)
    )
    return CrawlResult(
        captures=tuple(captures),
        errors=tuple(errors),
        canonical_home_url=canonical_home,
        frontier_exhausted=not queue,
    )
