from __future__ import annotations

import http.client
import ipaddress
import math
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from .parser import normalize_http_url, same_site


class WebsiteFetchError(RuntimeError):
    """A bounded website request could not be completed safely."""


class WebsiteBlockedError(WebsiteFetchError):
    """A source policy or safety rule blocked a website request."""


class _WebsiteTransientError(WebsiteFetchError):
    """A transport-level failure that may be retried within the configured budget."""


@dataclass
class _RequestBudget:
    attempt_limit: int
    retry_delay_budget_seconds: float
    attempts_started: int = 0
    retry_delay_used: float = 0.0

    def start_attempt(self, url: str) -> None:
        if self.attempts_started >= self.attempt_limit:
            raise WebsiteFetchError(
                f"request attempt limit exhausted ({self.attempt_limit}) for {url}"
            )
        self.attempts_started += 1

    def reserve_retry_delay(self, delay: float, url: str) -> None:
        projected = self.retry_delay_used + delay
        if projected > self.retry_delay_budget_seconds:
            raise WebsiteFetchError(
                "retry delay budget exhausted "
                f"({projected:g}s > {self.retry_delay_budget_seconds:g}s) for {url}"
            )
        self.retry_delay_used = projected


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
        request_interval_seconds: float = 1.0,
        max_policy_delay_seconds: float = 30.0,
        retry_attempt_limit: int = 4,
        retry_base_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 30.0,
        retry_delay_budget_seconds: float = 60.0,
        obey_robots: bool = True,
        dns_lookup=socket.getaddrinfo,
        ssl_context: ssl.SSLContext | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        normalized = normalize_http_url(site_url)
        if normalized is None:
            raise ValueError("site_url must be an absolute http/https URL")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be greater than zero")
        if (
            not math.isfinite(request_interval_seconds)
            or request_interval_seconds <= 0
        ):
            raise ValueError(
                "request_interval_seconds must be finite and greater than zero"
            )
        if (
            not math.isfinite(max_policy_delay_seconds)
            or max_policy_delay_seconds < request_interval_seconds
            or max_policy_delay_seconds > 300
        ):
            raise ValueError(
                "max_policy_delay_seconds must be finite, at least request_interval_seconds and at most 300"
            )
        if retry_attempt_limit < 1 or retry_attempt_limit > 8:
            raise ValueError("retry_attempt_limit must be between 1 and 8")
        if (
            not math.isfinite(retry_base_delay_seconds)
            or retry_base_delay_seconds <= 0
            or retry_base_delay_seconds > 60
        ):
            raise ValueError(
                "retry_base_delay_seconds must be finite, greater than zero and at most 60"
            )
        if (
            not math.isfinite(retry_max_delay_seconds)
            or retry_max_delay_seconds < retry_base_delay_seconds
            or retry_max_delay_seconds > 300
        ):
            raise ValueError(
                "retry_max_delay_seconds must be finite, at least retry_base_delay_seconds and at most 300"
            )
        if (
            not math.isfinite(retry_delay_budget_seconds)
            or retry_delay_budget_seconds < retry_max_delay_seconds
            or retry_delay_budget_seconds > 900
        ):
            raise ValueError(
                "retry_delay_budget_seconds must be finite, at least retry_max_delay_seconds and at most 900"
            )
        self.site_url = normalized
        self.user_agent = user_agent
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self.request_interval_seconds = float(request_interval_seconds)
        self.max_policy_delay_seconds = float(max_policy_delay_seconds)
        self.retry_attempt_limit = int(retry_attempt_limit)
        self.retry_base_delay_seconds = float(retry_base_delay_seconds)
        self.retry_max_delay_seconds = float(retry_max_delay_seconds)
        self.retry_delay_budget_seconds = float(retry_delay_budget_seconds)
        self.obey_robots = bool(obey_robots)
        self._dns_lookup = dns_lookup
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._monotonic = monotonic
        self._sleep = sleep
        self._wall_time = wall_time
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request_started: dict[str, float] = {}

    def _assert_allowed_site(self, url: str) -> None:
        if not same_site(url, self.site_url):
            raise WebsiteBlockedError(f"cross-site fetch blocked: {url}")
        site = urlsplit(self.site_url)
        candidate = urlsplit(url)
        site_port = self._port(site)
        candidate_port = self._port(candidate)
        if site.scheme == "https" and candidate.scheme != "https":
            raise WebsiteBlockedError(f"HTTPS downgrade blocked: {url}")
        if candidate.scheme == site.scheme:
            if candidate_port != site_port:
                raise WebsiteBlockedError(f"same-site port shift blocked: {url}")
            return
        if (
            site.scheme == "http"
            and candidate.scheme == "https"
            and site_port == 80
            and candidate_port == 443
        ):
            return
        raise WebsiteBlockedError(f"same-site scheme/port transition blocked: {url}")

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
                raise _WebsiteTransientError(
                    f"DNS resolution failed for {lowered}: {exc}"
                ) from exc
            for row in rows:
                sockaddr = row[4]
                if sockaddr:
                    addresses.add(str(sockaddr[0]))
        if not addresses:
            raise _WebsiteTransientError(
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

    def _policy_delay_seconds(self, url: str) -> float:
        delay = self.request_interval_seconds
        parser = self._robots.get(self._origin(url))
        if parser is not None:
            crawl_delay = parser.crawl_delay(self.user_agent)
            if crawl_delay is not None:
                delay = max(delay, float(crawl_delay))
            request_rate = parser.request_rate(self.user_agent)
            if request_rate is not None:
                if request_rate.requests <= 0 or request_rate.seconds <= 0:
                    raise WebsiteBlockedError(
                        "robots request-rate policy is non-positive for "
                        f"{self._origin(url)}"
                    )
                # Evenly spacing requests at seconds/requests is conservative:
                # it never exceeds the advertised average request rate.
                delay = max(
                    delay,
                    float(request_rate.seconds) / float(request_rate.requests),
                )
        if delay > self.max_policy_delay_seconds:
            raise WebsiteBlockedError(
                "robots pacing policy exceeds configured maximum "
                f"({delay:g}s > {self.max_policy_delay_seconds:g}s) for {self._origin(url)}"
            )
        return delay

    def _pace(self, url: str) -> None:
        origin = self._origin(url)
        delay = self._policy_delay_seconds(url)
        now = float(self._monotonic())
        previous = self._last_request_started.get(origin)
        if previous is not None:
            remaining = delay - (now - previous)
            if remaining > 0:
                self._sleep(remaining)
                observed = float(self._monotonic())
                # Test clocks and unusual sleep implementations may not advance
                # monotonically by the requested amount. Preserve the schedule
                # conservatively rather than allowing a subsequent early request.
                now = max(observed, previous + delay)
        self._last_request_started[origin] = now

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

    def _request_once(
        self,
        url: str,
        max_bytes: int,
        *,
        budget: _RequestBudget | None = None,
    ) -> tuple[int, Message, bytes]:
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
                self._pace(url)
                if budget is not None:
                    budget.start_attempt(url)
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

        raise _WebsiteTransientError(
            f"request failed for {url}: {last_error or 'all validated addresses failed'}"
        )

    def _retry_after_seconds(self, headers: Message, url: str) -> float | None:
        raw = headers.get("Retry-After")
        if raw is None:
            return None
        value = raw.strip()
        if not value:
            raise WebsiteFetchError(f"malformed Retry-After header for {url}")
        try:
            seconds = int(value, 10)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise WebsiteFetchError(
                    f"malformed Retry-After header for {url}: {value!r}"
                ) from exc
            if parsed is None or parsed.tzinfo is None:
                raise WebsiteFetchError(
                    f"malformed Retry-After header for {url}: {value!r}"
                )
            try:
                delay = max(0.0, float(parsed.timestamp()) - float(self._wall_time()))
            except (OverflowError, OSError, ValueError) as exc:
                raise WebsiteFetchError(
                    f"malformed Retry-After header for {url}: {value!r}"
                ) from exc
        else:
            if seconds < 0:
                raise WebsiteFetchError(
                    f"malformed Retry-After header for {url}: {value!r}"
                )
            try:
                delay = float(seconds)
            except OverflowError as exc:
                raise WebsiteFetchError(
                    f"Retry-After exceeds configured maximum for {url}"
                ) from exc
        if not math.isfinite(delay):
            raise WebsiteFetchError(f"malformed Retry-After header for {url}")
        if delay > self.retry_max_delay_seconds:
            raise WebsiteFetchError(
                "Retry-After exceeds configured maximum "
                f"({delay:g}s > {self.retry_max_delay_seconds:g}s) for {url}"
            )
        return delay

    def _retry_delay_seconds(
        self,
        *,
        retry_number: int,
        headers: Message | None,
        url: str,
    ) -> float:
        backoff = min(
            self.retry_max_delay_seconds,
            self.retry_base_delay_seconds * (2 ** max(0, retry_number - 1)),
        )
        retry_after = None if headers is None else self._retry_after_seconds(headers, url)
        return max(backoff, retry_after or 0.0)

    def _sleep_for_retry(
        self,
        *,
        budget: _RequestBudget,
        delay: float,
        url: str,
    ) -> None:
        budget.reserve_retry_delay(delay, url)
        if delay > 0:
            self._sleep(delay)

    def _request_with_retries(
        self, url: str, max_bytes: int
    ) -> tuple[int, Message, bytes]:
        budget = _RequestBudget(
            attempt_limit=self.retry_attempt_limit,
            retry_delay_budget_seconds=self.retry_delay_budget_seconds,
        )
        retry_number = 0
        while True:
            try:
                status, headers, body = self._request_once(
                    url, max_bytes, budget=budget
                )
            except _WebsiteTransientError as exc:
                failure_cycles = retry_number + 1
                if (
                    budget.attempts_started >= budget.attempt_limit
                    or failure_cycles >= budget.attempt_limit
                ):
                    raise WebsiteFetchError(
                        "request attempt limit exhausted after transport failures "
                        f"({budget.attempt_limit}) for {url}"
                    ) from exc
                retry_number += 1
                delay = self._retry_delay_seconds(
                    retry_number=retry_number,
                    headers=None,
                    url=url,
                )
                self._sleep_for_retry(budget=budget, delay=delay, url=url)
                continue

            if status not in {429, 503}:
                return status, headers, body
            if budget.attempts_started >= budget.attempt_limit:
                raise WebsiteFetchError(
                    f"request attempt limit exhausted after HTTP {status} "
                    f"({budget.attempt_limit}) for {url}"
                )
            retry_number += 1
            delay = self._retry_delay_seconds(
                retry_number=retry_number,
                headers=headers,
                url=url,
            )
            self._sleep_for_retry(budget=budget, delay=delay, url=url)

    def _robots_for(self, url: str) -> RobotFileParser | None:
        origin = self._origin(url)
        if origin in self._robots:
            return self._robots[origin]
        robots_url = urljoin(origin, "robots.txt")
        status, headers, body = self._request_with_retries(
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
            if redirected is None:
                raise WebsiteBlockedError(
                    f"invalid robots redirect target: {location}"
                )
            self._assert_allowed_site(redirected)
            status, headers, body = self._request_with_retries(
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
            status, headers, body = self._request_with_retries(
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
