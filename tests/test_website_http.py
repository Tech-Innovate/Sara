import socket

import pytest

from sara.website import CrawlConfig
from sara.website.http import SafeHttpClient, WebsiteBlockedError, _PinnedHTTPConnection


def _client(address: str) -> SafeHttpClient:
    def lookup(_host, _port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    return SafeHttpClient(
        site_url="https://example.com/",
        user_agent="SaraBusinessUnderstanding/1.0",
        timeout_seconds=2,
        max_response_bytes=65536,
        dns_lookup=lookup,
    )


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.1.1", "192.168.1.5", "::1"],
)
def test_private_or_local_resolution_is_blocked(address: str) -> None:
    client = _client(address)
    with pytest.raises(WebsiteBlockedError, match="non-public"):
        client._assert_public_host("https://example.com/")


def test_public_resolution_passes_ssrf_guard() -> None:
    client = _client("93.184.216.34")
    assert client._resolve_public_addresses("https://example.com/") == (
        "93.184.216.34",
    )


def test_mixed_public_and_private_dns_answers_fail_closed() -> None:
    def lookup(_host, _port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    client = SafeHttpClient(
        site_url="https://example.com/",
        user_agent="SaraBusinessUnderstanding/1.0",
        timeout_seconds=2,
        max_response_bytes=65536,
        dns_lookup=lookup,
    )
    with pytest.raises(WebsiteBlockedError, match="non-public"):
        client._resolve_public_addresses("https://example.com/")


def test_connection_uses_validated_address_not_hostname_dns(monkeypatch) -> None:
    called = []
    sentinel = object()

    def connect(address, timeout):
        called.append((address, timeout))
        return sentinel

    monkeypatch.setattr(socket, "create_connection", connect)
    connection = _PinnedHTTPConnection(
        "example.com", 80, "93.184.216.34", 2.5
    )
    connection.connect()
    assert connection.sock is sentinel
    assert called == [(('93.184.216.34', 80), 2.5)]


def test_cross_site_fetch_target_is_blocked_before_request() -> None:
    client = _client("93.184.216.34")
    with pytest.raises(WebsiteBlockedError, match="cross-site"):
        client._assert_allowed_site("https://evil.example.net/")


def test_same_site_boundary_blocks_https_downgrade_and_port_shift() -> None:
    client = _client("93.184.216.34")
    client._assert_allowed_site("https://www.example.com/path")
    with pytest.raises(WebsiteBlockedError, match="downgrade"):
        client._assert_allowed_site("http://example.com/path")
    with pytest.raises(WebsiteBlockedError, match="port shift"):
        client._assert_allowed_site("https://example.com:8443/path")


def test_same_site_boundary_allows_default_http_to_https_upgrade() -> None:
    client = SafeHttpClient(
        site_url="http://example.com/",
        user_agent="SaraBusinessUnderstanding/1.0",
        timeout_seconds=2,
        max_response_bytes=65536,
    )
    client._assert_allowed_site("https://www.example.com/path")
    with pytest.raises(WebsiteBlockedError, match="transition"):
        client._assert_allowed_site("https://example.com:8443/path")


def test_phase6_config_cannot_disable_robots_compliance() -> None:
    with pytest.raises(ValueError, match="robots"):
        CrawlConfig(obey_robots=False).validate()
