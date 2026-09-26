from __future__ import annotations

import socket
from email.message import Message

import pytest

from sara.website import http as website_http
from sara.website.http import SafeHttpClient, WebsiteFetchError


def _client(**overrides) -> SafeHttpClient:
    kwargs = {
        "site_url": "https://example.com/",
        "user_agent": "SaraBusinessUnderstanding/1.0",
        "timeout_seconds": 2,
        "max_response_bytes": 65536,
        "request_interval_seconds": 1.0,
        "max_policy_delay_seconds": 30.0,
        "retry_attempt_limit": 4,
        "retry_base_delay_seconds": 1.0,
        "retry_max_delay_seconds": 30.0,
        "retry_delay_budget_seconds": 60.0,
    }
    kwargs.update(overrides)
    return SafeHttpClient(**kwargs)


def _headers(**values: str) -> Message:
    result = Message()
    for key, value in values.items():
        result[key.replace("_", "-")] = value
    return result


def _scripted_request(client: SafeHttpClient, monkeypatch, steps):
    calls: list[str] = []

    def request(url: str, _max_bytes: int, *, budget=None):
        assert budget is not None
        budget.start_attempt(url)
        calls.append(url)
        step = steps[len(calls) - 1]
        if isinstance(step, BaseException):
            raise step
        status, headers = step
        return status, headers, b"body"

    monkeypatch.setattr(client, "_request_once", request)
    return calls


def test_503_retries_with_bounded_exponential_backoff(monkeypatch) -> None:
    sleeps: list[float] = []
    client = _client(sleep=sleeps.append)
    calls = _scripted_request(
        client,
        monkeypatch,
        [
            (503, _headers()),
            (503, _headers()),
            (200, _headers(Content_Type="text/html")),
        ],
    )

    status, _headers_result, _body = client._request_with_retries(
        "https://example.com/page", 65536
    )

    assert status == 200
    assert len(calls) == 3
    assert sleeps == pytest.approx([1.0, 2.0])


def test_429_honors_retry_after_delta_seconds(monkeypatch) -> None:
    sleeps: list[float] = []
    client = _client(sleep=sleeps.append)
    calls = _scripted_request(
        client,
        monkeypatch,
        [
            (429, _headers(Retry_After="7")),
            (200, _headers(Content_Type="text/html")),
        ],
    )

    status, _headers_result, _body = client._request_with_retries(
        "https://example.com/page", 65536
    )

    assert status == 200
    assert len(calls) == 2
    assert sleeps == pytest.approx([7.0])


def test_retry_after_http_date_uses_wall_clock(monkeypatch) -> None:
    sleeps: list[float] = []
    client = _client(sleep=sleeps.append, wall_time=lambda: 0.0)
    _scripted_request(
        client,
        monkeypatch,
        [
            (503, _headers(Retry_After="Thu, 01 Jan 1970 00:00:05 GMT")),
            (200, _headers(Content_Type="text/html")),
        ],
    )

    status, _headers_result, _body = client._request_with_retries(
        "https://example.com/page", 65536
    )

    assert status == 200
    assert sleeps == pytest.approx([5.0])


@pytest.mark.parametrize("value", ["not-a-date", "-1", ""])
def test_malformed_retry_after_fails_closed_without_retry(
    monkeypatch, value: str
) -> None:
    sleeps: list[float] = []
    client = _client(sleep=sleeps.append)
    calls = _scripted_request(
        client,
        monkeypatch,
        [(429, _headers(Retry_After=value))],
    )

    with pytest.raises(WebsiteFetchError, match="Retry-After"):
        client._request_with_retries("https://example.com/page", 65536)

    assert len(calls) == 1
    assert sleeps == []


def test_retry_after_above_configured_maximum_fails_closed(monkeypatch) -> None:
    sleeps: list[float] = []
    client = _client(
        retry_max_delay_seconds=5.0,
        retry_delay_budget_seconds=10.0,
        sleep=sleeps.append,
    )
    calls = _scripted_request(
        client,
        monkeypatch,
        [(503, _headers(Retry_After="6"))],
    )

    with pytest.raises(WebsiteFetchError, match="exceeds configured maximum"):
        client._request_with_retries("https://example.com/page", 65536)

    assert len(calls) == 1
    assert sleeps == []


def test_permanent_http_status_is_not_retried(monkeypatch) -> None:
    sleeps: list[float] = []
    client = _client(sleep=sleeps.append)
    calls = _scripted_request(
        client,
        monkeypatch,
        [(500, _headers())],
    )

    status, _headers_result, _body = client._request_with_retries(
        "https://example.com/page", 65536
    )

    assert status == 500
    assert len(calls) == 1
    assert sleeps == []


def test_cumulative_retry_delay_budget_is_hard_cap(monkeypatch) -> None:
    sleeps: list[float] = []
    client = _client(
        retry_attempt_limit=4,
        retry_base_delay_seconds=4.0,
        retry_max_delay_seconds=4.0,
        retry_delay_budget_seconds=6.0,
        sleep=sleeps.append,
    )
    calls = _scripted_request(
        client,
        monkeypatch,
        [
            (503, _headers()),
            (503, _headers()),
        ],
    )

    with pytest.raises(WebsiteFetchError, match="retry delay budget exhausted"):
        client._request_with_retries("https://example.com/page", 65536)

    assert len(calls) == 2
    assert sleeps == pytest.approx([4.0])


def test_retry_attempt_limit_stops_retryable_http_statuses(monkeypatch) -> None:
    sleeps: list[float] = []
    client = _client(retry_attempt_limit=2, sleep=sleeps.append)
    calls = _scripted_request(
        client,
        monkeypatch,
        [
            (503, _headers()),
            (503, _headers()),
        ],
    )

    with pytest.raises(WebsiteFetchError, match="attempt limit exhausted"):
        client._request_with_retries("https://example.com/page", 65536)

    assert len(calls) == 2
    assert sleeps == pytest.approx([1.0])


def test_validated_ip_failover_consumes_attempt_budget(monkeypatch) -> None:
    attempts: list[str] = []

    def lookup(_host, _port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"93.184.216.{i}", 443))
            for i in range(30, 35)
        ]

    class Connection:
        def __init__(
            self,
            _host: str,
            _port: int,
            address: str,
            _timeout: float,
            _context,
        ) -> None:
            self.address = address

        def request(self, _method: str, _target: str, *, headers) -> None:
            attempts.append(self.address)
            raise OSError("simulated transport failure")

        def close(self) -> None:
            pass

    monkeypatch.setattr(website_http, "_PinnedHTTPSConnection", Connection)
    client = _client(
        retry_attempt_limit=3,
        dns_lookup=lookup,
        monotonic=lambda: 100.0,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(WebsiteFetchError, match="attempt limit exhausted"):
        client._request_with_retries("https://example.com/page", 65536)

    assert len(attempts) == 3


def test_repeated_dns_failures_are_bounded(monkeypatch) -> None:
    lookups = 0
    sleeps: list[float] = []

    def lookup(_host, _port, **_kwargs):
        nonlocal lookups
        lookups += 1
        raise socket.gaierror("temporary resolver failure")

    client = _client(
        retry_attempt_limit=3,
        dns_lookup=lookup,
        sleep=sleeps.append,
    )

    with pytest.raises(WebsiteFetchError, match="attempt limit exhausted"):
        client._request_with_retries("https://example.com/page", 65536)

    assert lookups == 3
    assert sleeps == pytest.approx([1.0, 2.0])
