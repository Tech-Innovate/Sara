import socket

import pytest

from sara.website import CrawlConfig
from sara.website.http import SafeHttpClient, WebsiteBlockedError


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
    client._assert_public_host("https://example.com/")


def test_cross_site_fetch_target_is_blocked_before_request() -> None:
    client = _client("93.184.216.34")
    with pytest.raises(WebsiteBlockedError, match="cross-site"):
        client._assert_allowed_site("https://evil.example.net/")


def test_phase6_config_cannot_disable_robots_compliance() -> None:
    with pytest.raises(ValueError, match="robots"):
        CrawlConfig(obey_robots=False).validate()
