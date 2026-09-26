from __future__ import annotations

import json
import socket
from email.message import Message
from pathlib import Path

from sara.migrations import apply_migrations
from sara.storage import connect
from sara.website import http as website_http
from sara.website.http import SafeHttpClient
from sara.website.model import COLLECTOR_VERSION, CrawlConfig
from sara.website.target import begin_session


def test_acquisition_session_freezes_pacing_configuration_and_collector_version(
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
    assert COLLECTOR_VERSION == "2"
    assert row[0] == "2"
    assert frozen["request_interval_seconds"] == 1.5
    assert frozen["max_policy_delay_seconds"] == 25.0
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
