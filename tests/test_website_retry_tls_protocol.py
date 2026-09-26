from __future__ import annotations

import socket
import ssl

import pytest

from sara.website import http as website_http
from sara.website.http import SafeHttpClient, WebsiteFetchError


def test_generic_tls_protocol_failure_is_not_retried(monkeypatch) -> None:
    attempts = 0
    sleeps: list[float] = []

    def lookup(_host, _port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]

    class Connection:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def request(self, _method: str, _target: str, *, headers) -> None:
            nonlocal attempts
            attempts += 1
            raise ssl.SSLError(1, "wrong version number")

        def close(self) -> None:
            pass

    monkeypatch.setattr(website_http, "_PinnedHTTPSConnection", Connection)
    client = SafeHttpClient(
        site_url="https://example.com/",
        user_agent="SaraBusinessUnderstanding/1.0",
        timeout_seconds=2,
        max_response_bytes=65536,
        retry_attempt_limit=4,
        retry_base_delay_seconds=1.0,
        retry_max_delay_seconds=30.0,
        retry_delay_budget_seconds=60.0,
        dns_lookup=lookup,
        monotonic=lambda: 100.0,
        sleep=sleeps.append,
    )

    with pytest.raises(WebsiteFetchError, match="TLS protocol failure"):
        client._request_with_retries("https://example.com/page", 65536)

    assert attempts == 1
    assert sleeps == []
