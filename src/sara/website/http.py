from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from email.message import Message
from urllib.parse import urljoin, urlsplit, urlunsplit
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


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        address: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._pinned_address = address

    def connect(self) -> None:
        # Connect to the exact address that passed the SSRF check, while keeping
        # the original hostname for TLS SNI and certificate verification.
        raw = socket.create_connection(
            (self._pinned_address, self.port), self.timeout
        )
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


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
        ssl_context: ssl.SSLContext | None = None,
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
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._robots: dict[str, RobotFileParser | None] = {}

    def _assert_allowed_site(self, url: str) -> None:
        if not same_site(url, self.site_url):
            raise WebsiteBlockedError(f"cross-site fetch blocked: {url}")

    @staticmethod
    def _port(parsed) -> int:  # noqa: ANN001
        if parsed.port is not None:
            return int(parsed.port)
        return 443 if parsed.scheme == "https" else 80

    def _resolve_public_addresses(self, url: str) -> tuple[str, ...]:
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            raise WebsiteBlockedError(f"URL has no hostname: {url}")
        lowered = host.lower().rstrip(".")
        if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(
            ".localhost"
        ):
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
                rows = self._dns_lookup(
                    lowered, self._port(parsed), type=socket.SOCK_STREAM
                )
            except OSError as exc:
                raise WebsiteFetchError(
                    f"DNS resolution failed for {lowered}: {exc}"
                ) from exc
            for row in rows:
                sockaddr = row[4]
                if sockaddr:
                    addresses.add(str(sockaddr[0]))
        if not addresses:
            raise WebsiteFetchError(
                f"DNS resolution returned no addresses for {lowered}"
            )

        checked: list[str] = []
        for value in sorted(addresses):
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise WebsiteBlockedError(
                    f"unrecognized resolved address for {lowered}: {value}"
                ) from exc
            if not address.is_global:
                # Reject mixed public/private DNS answers too. The request must
                # not be able to fall through to an internal address.
                raise WebsiteBlockedError(
                    f"non-public resolved address blocked for {lowered}: {address}"
                )
            checked.append(str(address))
        return tuple(checked)

    # Kept as a small explicit assertion surface for tests and callers that need
    # to validate a URL without issuing a request.
    def _assert_public_host(self, url: str) -> None:
        self._resolve_public_addresses(url)

    def _origin(self, url: str) -> str:
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

    @staticmethod
    def _request_target(parsed) -> str:  # noqa: ANN001
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        return target

    @staticmethod
    def _host_header(parsed) -> str:  # noqa: ANN001
        host = parsed.hostname or ""
        display_host = f"[{host}]" if ":" in host else host
        port = parsed.port
        if port and not (
            (parsed.scheme == "http" and port == 80)
            or (parsed.scheme == "https" and port == 443)
        ):
            return f"{display_host}:{port}"
        return display_host

    def _request_once(self, url: str, max_bytes: int) -> tuple[int, Message, bytes]:
        self._assert_allowed_site(url)
        parsed = urlsplit(url)
        addresses = self._resolve_public_addresses(url)
        port = self._port(parsed)
        target = self._request_target(parsed)
        host_header = self._host_header(parsed)
        last_error: BaseException | None = None

        for address in addresses:
            if parsed.scheme == "https":
                connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
                    parsed.hostname or "",
                    port,
                    address,
                    self.timeout_seconds,
                    self._ssl_context,
                )
            else:
                connection = _PinnedHTTPConnection(
                    parsed.hostname or "",
                    port,
                    address,
                    self.timeout_seconds,
                )
            try:
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Host": host_header,
                        "User-Agent": self.user_agent,
                        "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.2,*/*;q=0.1",
                        "Connection": "close",
                    },
                )
                response = connection.getresponse()
                status = int(response.status)
                headers = response.headers
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise WebsiteFetchError(
                        f"response exceeded configured byte limit ({max_bytes}) for {url}"
                    )
                return status, headers, body
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
            finally:
                connection.close()

        raise WebsiteFetchError(
            f"request failed for {url}: {last_error or 'all validated addresses failed'}"
        )

    def _robots_for(self, url: str) -> RobotFileParser | None:
        origin = self._origin(url)
        if origin in self._robots:
            return self._robots[origin]
        robots_url = urljoin(origin, "robots.txt")
        status, headers, body = self._request_once(
            robots_url, min(262_144, self.max_response_bytes)
        )
        if status in {404, 410}:
            self._robots[origin] = None
            return None
        if status in {401, 403}:
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(["User-agent: *", "Disallow: /"])
            self._robots[origin] = parser
            return parser
        if 300 <= status < 400:
            location = headers.get("Location")
            if not location:
                raise WebsiteFetchError(
                    f"robots redirect for {origin} has no Location header"
                )
            redirected = normalize_http_url(location, robots_url)
            if redirected is None or not same_site(redirected, self.site_url):
                raise WebsiteBlockedError(
                    f"cross-site robots redirect blocked: {location}"
                )
            status, headers, body = self._request_once(
                redirected, min(262_144, self.max_response_bytes)
            )
            if status in {404, 410}:
                self._robots[origin] = None
                return None
            if status in {401, 403}:
                parser = RobotFileParser()
                parser.set_url(redirected)
                parser.parse(["User-agent: *", "Disallow: /"])
                self._robots[origin] = parser
                return parser
        if status != 200:
            raise WebsiteFetchError(
                f"robots policy could not be established for {origin}: HTTP {status}"
            )
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
            status, headers, body = self._request_once(
                current, self.max_response_bytes
            )
            if 300 <= status < 400:
                location = headers.get("Location")
                if not location:
                    raise WebsiteFetchError(
                        f"HTTP redirect has no Location header for {current}"
                    )
                target = normalize_http_url(location, current)
                if target is None:
                    raise WebsiteBlockedError(
                        f"invalid redirect target from {current}: {location}"
                    )
                self._assert_allowed_site(target)
                if not self.robots_allowed(target):
                    raise WebsiteBlockedError(
                        f"robots policy disallows redirect target {target}"
                    )
                current = target
                continue
            if status != 200:
                raise WebsiteFetchError(f"unexpected HTTP {status} for {current}")
            media_type, charset = self._media_type(headers)
            if media_type not in {"text/html", "application/xhtml+xml"}:
                raise WebsiteFetchError(
                    f"unsupported media type {media_type!r} for {current}"
                )
            return HttpResponse(
                requested_url=normalized,
                final_url=current,
                status=status,
                headers={key.lower(): value for key, value in headers.items()},
                body=body,
                media_type=media_type,
                charset=charset,
            )
        raise WebsiteFetchError(
            f"too many redirects while fetching {normalized}"
        )
