from __future__ import annotations

import json
import socket
from email.message import Message
from pathlib import Path

import pytest

from sara.migrations import apply_migrations
from sara.storage import connect
from sara.website import http as website_http
from sara.website.http import SafeHttpClient
from sara.website.model import COLLECTOR_VERSION, CrawlConfig
from sara.website.target import begin_session


def test_acquisition_session_freezes_transport_configuration_and_collector_version(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "pacing.sqlite")
    apply_migrations(conn)
    created_at = "2026-09-26T10:00:00+00:00"
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) "
        "VALUES ('be_pacing','business_entity',?,?)",
        (created_at, created_at),
    )
    conn.execute(
        "INSERT INTO business_entities(id,display_name,created_at,updated_at) "
        "VALUES ('be_pacing','Pacing Test',?,?)",
        (created_at, created_at),
    )
    conn.commit()

    config = CrawlConfig(
        request_interval_seconds=1.5,
        max_policy_delay_seconds=25.0,
        retry_attempt_limit=5,
        retry_base_delay_seconds=2.0,
        retry_max_delay_seconds=20.0,
        retry_delay_budget_seconds=50.0,
    )
    config.validate()
    session_id = begin_session(
        conn,
        entity_id="be_pacing",
        start_url="https://example.com/",
        evidence_root=tmp_path / "evidence",
        config=config,
        started_at=created_at,
    )
    row = conn.execute(
        "SELECT collector_version,config_json FROM acquisition_sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    frozen = json.loads(row[1])
    assert COLLECTOR_VERSION == "3"
    assert row[0] == "3"
    assert frozen["request_interval_seconds"] == 1.5
    assert frozen["max_policy_delay_seconds"] == 25.0
    assert frozen["retry_attempt_limit"] == 5
    assert frozen["retry_base_delay_seconds"] == 2.0
    assert frozen["retry_max_delay_seconds"] == 20.0
    assert frozen["retry_delay_budget_seconds"] == 50.0
    conn.close()


def test_each_validated_address_request_attempt_is_paced(monkeypatch) -> None:
    def lookup(_host, _port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 443)),
        ]

    class Response:
        status = 200

        def __init__(self) -> None:
            self.headers = Message()
            self.headers["Content-Type"] = "text/html; charset=utf-8"

        def read(self, _limit: int) -> bytes:
            return b"<html></html>"

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
            assert headers["User-Agent"] == "SaraBusinessUnderstanding/1.0"
            if self.address == "93.184.216.34":
                raise OSError("first validated address failed")

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(website_http, "_PinnedHTTPSConnection", Connection)
    client = SafeHttpClient(
        site_url="https://example.com/",
        user_agent="SaraBusinessUnderstanding/1.0",
        timeout_seconds=2,
        max_response_bytes=65536,
        dns_lookup=lookup,
    )
    paced: list[str] = []
    monkeypatch.setattr(client, "_pace", lambda url: paced.append(url))

    status, _headers, body = client._request_once("https://example.com/", 65536)
    assert status == 200
    assert body == b"<html></html>"
    assert paced == ["https://example.com/", "https://example.com/"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"timeout_seconds": float("nan")}, "timeout_seconds"),
        ({"request_interval_seconds": float("nan")}, "request_interval_seconds"),
        ({"max_policy_delay_seconds": float("nan")}, "max_policy_delay_seconds"),
        ({"request_interval_seconds": float("inf")}, "request_interval_seconds"),
        ({"max_policy_delay_seconds": float("inf")}, "max_policy_delay_seconds"),
        ({"retry_base_delay_seconds": float("nan")}, "retry_base_delay_seconds"),
        ({"retry_max_delay_seconds": float("nan")}, "retry_max_delay_seconds"),
        ({"retry_delay_budget_seconds": float("nan")}, "retry_delay_budget_seconds"),
        ({"retry_base_delay_seconds": float("inf")}, "retry_base_delay_seconds"),
        ({"retry_max_delay_seconds": float("inf")}, "retry_max_delay_seconds"),
        ({"retry_delay_budget_seconds": float("inf")}, "retry_delay_budget_seconds"),
    ],
)
def test_crawl_config_rejects_non_finite_timing_values(kwargs, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        CrawlConfig(**kwargs).validate()


def test_http_client_rejects_non_finite_timing_values() -> None:
    common = {
        "site_url": "https://example.com/",
        "user_agent": "SaraBusinessUnderstanding/1.0",
        "timeout_seconds": 2,
        "max_response_bytes": 65536,
    }
    with pytest.raises(ValueError, match="request_interval_seconds"):
        SafeHttpClient(**common, request_interval_seconds=float("nan"))
    with pytest.raises(ValueError, match="max_policy_delay_seconds"):
        SafeHttpClient(**common, max_policy_delay_seconds=float("nan"))
    with pytest.raises(ValueError, match="retry_base_delay_seconds"):
        SafeHttpClient(**common, retry_base_delay_seconds=float("nan"))
    with pytest.raises(ValueError, match="retry_max_delay_seconds"):
        SafeHttpClient(**common, retry_max_delay_seconds=float("nan"))
    with pytest.raises(ValueError, match="retry_delay_budget_seconds"):
        SafeHttpClient(**common, retry_delay_budget_seconds=float("nan"))
