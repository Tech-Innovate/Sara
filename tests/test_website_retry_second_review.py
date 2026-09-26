from __future__ import annotations

import http.client
import socket
from email.message import Message

import pytest

from sara.website import http as website_http
from sara.website.http import SafeHttpClient, WebsiteFetchError


def _lookup(_host, _port, **_kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    ]


def _client(*, sleep) -> SafeHttpClient:
    return SafeHttpClient(
        site_url="https://example.com/",
        user_agent="SaraBusinessUnderstanding/1.0",
        timeout_seconds=2,
        max_response_bytes=65536,
        request_interval_seconds=1.0,
        max_policy_delay_seconds=30.0,
        retry_attempt_limit=2,
        retry_base_delay_seconds=1.0,
        retry_max_delay_seconds=1.0,
        retry_delay_budget_seconds=1.0,
        dns_lookup=_lookup,
        sleep=sleep,
    )


def _headers() -> Message:
    headers = Message()
    headers["Content-Type"] = "text/html; charset=utf-8"
    return headers


def test_retryable_status_is_not_preempted_by_oversized_error_body(monkeypatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    class RetryableResponse:
        status = 503
        headers = _headers()

        def read(self, _limit: int) -> bytes:
            raise AssertionError("retryable response body must not be read")

    class SuccessResponse:
        status = 200
        headers = _headers()

        def read(self, _limit: int) -> bytes:
            return b"<html>ok</html>"

    responses = [RetryableResponse(), SuccessResponse()]

    class Connection:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def request(self, _method: str, _target: str, *, headers) -> None:
            nonlocal attempts
            attempts += 1

        def getresponse(self):
            return responses.pop(0)

        def close(self) -> None:
            pass

    monkeypatch.setattr(website_http, "_PinnedHTTPSConnection", Connection)
    client = _client(sleep=sleeps.append)
    monkeypatch.setattr(client, "_pace", lambda _url: None)

    status, _headers_result, body = client._request_with_retries(
        "https://example.com/page", 65536
    )

    assert status == 200
    assert body == b"<html>ok</html>"
    assert attempts == 2
    assert sleeps == pytest.approx([1.0])


def test_non_transient_http_protocol_failure_is_not_retried(monkeypatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    class Connection:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def request(self, _method: str, _target: str, *, headers) -> None:
            nonlocal attempts
            attempts += 1

        def getresponse(self):
            raise http.client.BadStatusLine("garbled status")

        def close(self) -> None:
            pass

    monkeypatch.setattr(website_http, "_PinnedHTTPSConnection", Connection)
    client = _client(sleep=sleeps.append)
    monkeypatch.setattr(client, "_pace", lambda _url: None)

    with pytest.raises(WebsiteFetchError, match="HTTP protocol failure"):
        client._request_with_retries("https://example.com/page", 65536)

    assert attempts == 1
    assert sleeps == []


def test_incomplete_read_remains_retryable(monkeypatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    class TruncatedResponse:
        status = 200
        headers = _headers()

        def read(self, _limit: int) -> bytes:
            raise http.client.IncompleteRead(b"partial", 10)

    class SuccessResponse:
        status = 200
        headers = _headers()

        def read(self, _limit: int) -> bytes:
            return b"<html>ok</html>"

    responses = [TruncatedResponse(), SuccessResponse()]

    class Connection:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def request(self, _method: str, _target: str, *, headers) -> None:
            nonlocal attempts
            attempts += 1

        def getresponse(self):
            return responses.pop(0)

        def close(self) -> None:
            pass

    monkeypatch.setattr(website_http, "_PinnedHTTPSConnection", Connection)
    client = _client(sleep=sleeps.append)
    monkeypatch.setattr(client, "_pace", lambda _url: None)

    status, _headers_result, body = client._request_with_retries(
        "https://example.com/page", 65536
    )

    assert status == 200
    assert body == b"<html>ok</html>"
    assert attempts == 2
    assert sleeps == pytest.approx([1.0])
