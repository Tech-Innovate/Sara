from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..migrations import MigrationError, apply_migrations
from ..storage import connect_existing
from ..understanding_vocabulary import VocabularySeedError, seed_business_understanding_vocabulary
from .crawl import crawl_official_site
from .http import SafeHttpClient, WebsiteBlockedError, WebsiteFetchError
from .model import CrawlConfig, WebsiteAcquisitionError, WebsiteAcquisitionStats
from .persistence import ingest_crawl_result
from .target import begin_session, fail_session, resolve_verified_site


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def collect_official_website(
    conn: sqlite3.Connection,
    *,
    evidence_root: str | Path,
    business_id: int | None = None,
    canonical_key: str | None = None,
    entity_id: str | None = None,
    config: CrawlConfig | None = None,
    now: Callable[[], str] = _utc_now,
    client_factory: Callable[..., SafeHttpClient] = SafeHttpClient,
) -> WebsiteAcquisitionStats:
    if sum(value is not None for value in (business_id, canonical_key, entity_id)) != 1:
        raise WebsiteAcquisitionError(
            "select exactly one of business_id, canonical_key, or entity_id"
        )
    if business_id is not None and business_id <= 0:
        raise WebsiteAcquisitionError("business_id must be greater than zero")
    if canonical_key is not None and not canonical_key.strip():
        raise WebsiteAcquisitionError("canonical_key must not be blank")
    if entity_id is not None and not entity_id.strip():
        raise WebsiteAcquisitionError("entity_id must not be blank")
    if conn.in_transaction:
        raise WebsiteAcquisitionError(
            "website acquisition requires a connection with no active transaction"
        )
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise WebsiteAcquisitionError(
            "SQLite foreign-key enforcement must be enabled for website acquisition"
        )

    config = config or CrawlConfig()
    config.validate()
    root = Path(evidence_root)
    entity, start_url = resolve_verified_site(
        conn,
        business_id=business_id,
        canonical_key=canonical_key.strip() if canonical_key is not None else None,
        entity_id=entity_id.strip() if entity_id is not None else None,
    )
    started_at = now()
    session_id = begin_session(
        conn,
        entity_id=entity,
        start_url=start_url,
        evidence_root=root,
        config=config,
        started_at=started_at,
    )

    try:
        client = client_factory(
            site_url=start_url,
            user_agent=config.user_agent,
            timeout_seconds=config.timeout_seconds,
            max_response_bytes=config.max_response_bytes,
            obey_robots=config.obey_robots,
        )
        result = crawl_official_site(
            entity_id=entity,
            session_id=session_id,
            start_url=start_url,
            evidence_root=root,
            config=config,
            client=client,
            now=now,
        )
    except KeyboardInterrupt:
        fail_session(
            conn,
            session_id=session_id,
            status="cancelled",
            finished_at=now(),
            error="website acquisition interrupted",
        )
        raise
    except BaseException as exc:
        fail_session(
            conn,
            session_id=session_id,
            status="failed",
            finished_at=now(),
            error=str(exc),
        )
        raise

    if not result.captures:
        error = "; ".join(result.errors) or "website acquisition produced no usable pages"
        blocked = bool(result.errors) and all(
            "blocked" in item.lower() or "robots" in item.lower()
            for item in result.errors
        )
        fail_session(
            conn,
            session_id=session_id,
            status="blocked" if blocked else "failed",
            finished_at=now(),
            error=error,
        )
        raise WebsiteAcquisitionError(error)

    finished_at = now()
    try:
        persisted = ingest_crawl_result(
            conn,
            session_id=session_id,
            entity_id=entity,
            start_url=start_url,
            result=result,
            finished_at=finished_at,
        )
    except BaseException as exc:
        try:
            fail_session(
                conn,
                session_id=session_id,
                status="failed",
                finished_at=now(),
                error=f"ingestion failed: {exc}",
            )
        except BaseException:
            pass
        raise

    return WebsiteAcquisitionStats(
        session_id=session_id,
        business_entity_id=entity,
        start_url=start_url,
        canonical_home_url=result.canonical_home_url,
        status=str(persisted["status"]),
        pages_fetched=len(result.captures),
        evidence_items_created=int(persisted["evidence_items_created"]),
        observations_created=int(persisted["observations_created"]),
        channels_created=int(persisted["channels_created"]),
        channels_refreshed=int(persisted["channels_refreshed"]),
        facts_created=int(persisted["facts_created"]),
        facts_replaced=int(persisted["facts_replaced"]),
        fact_support_links_created=int(persisted["fact_support_links_created"]),
        not_observed_facts_created=int(persisted["not_observed_facts_created"]),
        fetch_errors=result.errors,
        unresolved_predicates=tuple(persisted["unresolved_predicates"]),
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sara-website",
        description="Run bounded official-website acquisition into Business Understanding.",
    )
    parser.add_argument("--db", default="data/sara.db", help="SQLite database path")
    parser.add_argument(
        "--evidence-dir",
        default="evidence/web",
        help="directory for retained raw website HTML",
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--business-id", type=_positive_int)
    selector.add_argument("--canonical-key")
    selector.add_argument("--entity-id")
    parser.add_argument("--page-limit", type=_positive_int, default=8)
    parser.add_argument("--depth-limit", type=int, default=2)
    parser.add_argument("--max-response-bytes", type=_positive_int, default=1_048_576)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--user-agent", default="SaraBusinessUnderstanding/1.0")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_existing(Path(args.db))
        apply_migrations(conn)
        seed_business_understanding_vocabulary(conn)
        stats = collect_official_website(
            conn,
            evidence_root=Path(args.evidence_dir),
            business_id=args.business_id,
            canonical_key=args.canonical_key,
            entity_id=args.entity_id,
            config=CrawlConfig(
                page_limit=args.page_limit,
                depth_limit=args.depth_limit,
                max_response_bytes=args.max_response_bytes,
                timeout_seconds=args.timeout,
                user_agent=args.user_agent,
            ),
        )
        options = {"ensure_ascii": False, "sort_keys": True}
        payload = asdict(stats)
        if args.pretty:
            print(json.dumps(payload, indent=2, **options))
        else:
            print(json.dumps(payload, separators=(",", ":"), **options))
        return 0 if stats.status == "complete" else 3
    except KeyboardInterrupt:
        print("website acquisition interrupted", file=sys.stderr)
        return 130
    except (
        FileNotFoundError,
        MigrationError,
        VocabularySeedError,
        WebsiteAcquisitionError,
        WebsiteBlockedError,
        WebsiteFetchError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()
