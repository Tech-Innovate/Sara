from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from .parser import normalize_http_url, same_site


class WebsiteFetchError(RuntimeError):
    """A bounded website request could not be completed safely."""


class WebsiteBlockedError(WebsiteFetchError):
    """A source policy or safety rule blocked a website request."""


@dataclass(frozen=True)
class HttpResponse:
    requested_url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    media_type: str
    charset: str

    @property
    def text(self) -> str:
        return self.body.decode(self.charset, errors="replace")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class SafeHttpClient:
    def __init__(
        self,
        *,
        site_url: str,
        user_agent: str,
        timeout_seconds: float,
        max_response_bytes: int,
        obey_robots: bool = True,
        dns_lookup=socket.getaddrinfo,
    ) -> None:
        normalized = normalize_http_url(site_url)
        if normalized is None:
            raise ValueError("site_url must be an absolute http/https URL")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be greater than zero")
        self.site_url = normalized
        self.user_agent = user_agent
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self.obey_robots = bool(obey_robots)
        self._dns_lookup = dns_lookup
        self._opener = build_opener(_NoRedirect())
        self._robots: dict[str, RobotFileParser | None] = {}

    def _assert_allowed_site(self, url: str) -> None:
        if not same_site(url, self.site_url):
            raise WebsiteBlockedError(f"cross-site fetch blocked: {url}")

    def _assert_public_host(self, url: str) -> None:
        host = urlsplit(url).hostname
        if not host:
            raise WebsiteBlockedError(f"URL has no hostname: {url}")
        lowered = host.lower().rstrip(".")
        if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost"):
            raise WebsiteBlockedError(f"local hostname blocked: {host}")
        try:
            literal = ipaddress.ip_address(lowered)
        except ValueError:
            literal = None
        addresses: set[str] = set()
        if literal is not None:
            addresses.add(str(literal))
        else:
            try:
                rows = self._dns_lookup(lowered, None, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise WebsiteFetchError(f"DNS resolution failed for {lowered}: {exc}") from exc
            for row in rows:
                sockaddr = row[4]
                if sockaddr:
                    addresses.add(str(sockaddr[0]))
        if not addresses:
            raise WebsiteFetchError(f"DNS resolution returned no addresses for {lowered}")
        for value in sorted(addresses):
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise WebsiteBlockedError(f"unrecognized resolved address for {lowered}: {value}") from exc
            if not address.is_global:
                raise WebsiteBlockedError(
                    f"non-public resolved address blocked for {lowered}: {address}"
                )

    def _origin(self, url: str) -> str:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
        if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
            netloc = f"{host}:{port}"
        else:
            netloc = host
        return urlunsplit((parsed.scheme, netloc, "/", "", ""))

    def _request_once(self, url: str, max_bytes: int) -> tuple[int, Message, bytes]:
        self._assert_allowed_site(url)
        self._assert_public_host(url)
        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.2,*/*;q=0.1",
                "Connection": "close",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                headers = response.headers
                body = response.read(max_bytes + 1)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                return int(exc.code), exc.headers, b""
            raise WebsiteFetchError(f"HTTP {exc.code} for {url}") from exc
        except URLError as exc:
            raise WebsiteFetchError(f"request failed for {url}: {exc.reason}") from exc
        except OSError as exc:
            raise WebsiteFetchError(f"request failed for {url}: {exc}") from exc
        if len(body) > max_bytes:
            raise WebsiteFetchError(
                f"response exceeded configured byte limit ({max_bytes}) for {url}"
            )
        return status, headers, body

    def _robots_for(self, url: str) -> RobotFileParser | None:
        origin = self._origin(url)
        if origin in self._robots:
            return self._robots[origin]
        robots_url = urljoin(origin, "robots.txt")
        try:
            status, _headers, body = self._request_once(robots_url, min(262_144, self.max_response_bytes))
        except WebsiteFetchError as exc:
            message = str(exc)
            if "HTTP 404" in message or "HTTP 410" in message:
                self._robots[origin] = None
                return None
            if "HTTP 401" in message or "HTTP 403" in message:
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(["User-agent: *", "Disallow: /"])
                self._robots[origin] = parser
                return parser
            raise WebsiteFetchError(f"robots policy could not be established for {origin}: {exc}") from exc
        if 300 <= status < 400:
            location = _headers.get("Location")
            if not location:
                raise WebsiteFetchError(f"robots redirect for {origin} has no Location header")
            redirected = normalize_http_url(location, robots_url)
            if redirected is None or not same_site(redirected, self.site_url):
                raise WebsiteBlockedError(f"cross-site robots redirect blocked: {location}")
            try:
                status, _headers, body = self._request_once(
                    redirected, min(262_144, self.max_response_bytes)
                )
            except WebsiteFetchError as exc:
                raise WebsiteFetchError(
                    f"robots policy could not be established after redirect for {origin}: {exc}"
                ) from exc
        if status != 200:
            raise WebsiteFetchError(f"unexpected robots HTTP {status} for {origin}")
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(body.decode("utf-8", errors="replace").splitlines())
        self._robots[origin] = parser
        return parser

    def robots_allowed(self, url: str) -> bool:
        if not self.obey_robots:
            return True
        parser = self._robots_for(url)
        return True if parser is None else bool(parser.can_fetch(self.user_agent, url))

    @staticmethod
    def _media_type(headers: Message) -> tuple[str, str]:
        content_type = headers.get_content_type().lower()
        charset = headers.get_content_charset() or "utf-8"
        return content_type, charset

    def fetch(self, url: str, *, max_redirects: int = 5) -> HttpResponse:
        normalized = normalize_http_url(url)
        if normalized is None:
            raise WebsiteBlockedError(f"invalid fetch URL: {url}")
        current = normalized
        if not self.robots_allowed(current):
            raise WebsiteBlockedError(f"robots policy disallows {current}")
        for _ in range(max_redirects + 1):
            status, headers, body = self._request_once(current, self.max_response_bytes)
            if 300 <= status < 400:
                location = headers.get("Location")
                if not location:
                    raise WebsiteFetchError(f"HTTP redirect has no Location header for {current}")
                target = normalize_http_url(location, current)
                if target is None:
                    raise WebsiteBlockedError(f"invalid redirect target from {current}: {location}")
                self._assert_allowed_site(target)
                if not self.robots_allowed(target):
                    raise WebsiteBlockedError(f"robots policy disallows redirect target {target}")
                current = target
                continue
            if status != 200:
                raise WebsiteFetchError(f"unexpected HTTP {status} for {current}")
            media_type, charset = self._media_type(headers)
            if media_type not in {"text/html", "application/xhtml+xml"}:
                raise WebsiteFetchError(f"unsupported media type {media_type!r} for {current}")
            return HttpResponse(
                requested_url=normalized,
                final_url=current,
                status=status,
                headers={key.lower(): value for key, value in headers.items()},
                body=body,
                media_type=media_type,
                charset=charset,
            )
        raise WebsiteFetchError(f"too many redirects while fetching {normalized}")
