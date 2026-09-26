from __future__ import annotations

import socket

import pytest

from sara.website import http as website_http
from sara.website.http import SafeHttpClient, WebsiteFetchError


def test_validated_ip_failover_uses_shared_exponential_backoff(monkeypatch) -> None:
    attempts: list[str] = []
    sleeps: list[float] = []
    open_connections = 0

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
            nonlocal open_connections
            self.address = address
            self.closed = False
            open_connections += 1

        def request(self, _method: str, _target: str, *, headers) -> None:
            attempts.append(self.address)
            raise OSError("simulated transport failure")

        def close(self) -> None:
            nonlocal open_connections
            if not self.closed:
                self.closed = True
                open_connections -= 1

    def sleep(seconds: float) -> None:
        assert open_connections == 0
        sleeps.append(seconds)

    monkeypatch.setattr(website_http, "_PinnedHTTPSConnection", Connection)
    client = SafeHttpClient(
        site_url="https://example.com/",
        user_agent="SaraBusinessUnderstanding/1.0",
        timeout_seconds=2,
        max_response_bytes=65536,
        request_interval_seconds=0.1,
        max_policy_delay_seconds=30.0,
        retry_attempt_limit=3,
        retry_base_delay_seconds=1.0,
        retry_max_delay_seconds=30.0,
        retry_delay_budget_seconds=60.0,
        dns_lookup=lookup,
        sleep=sleep,
    )
    monkeypatch.setattr(client, "_pace", lambda _url: None)

    with pytest.raises(WebsiteFetchError, match="attempt limit exhausted"):
        client._request_with_retries("https://example.com/page", 65536)

    assert len(attempts) == 3
    assert sleeps == pytest.approx([1.0, 2.0])
    assert open_connections == 0
