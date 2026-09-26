from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from .parser import ParsedPage


class WebsiteAcquisitionError(RuntimeError):
    """Official website acquisition cannot proceed safely."""


OFFICIAL_WEB_SOURCE_ID = "src_official_web"
COLLECTOR_NAME = "sara.website"
COLLECTOR_VERSION = "3"
RECONCILIATION_VERSION = "official-web-v1"
ID_NAMESPACE = "sara.business-understanding.official-web.v1"
CAPABILITY_PREDICATES = (
    "capability.online_booking",
    "capability.online_ordering",
    "capability.whatsapp",
)


@dataclass(frozen=True)
class CrawlConfig:
    page_limit: int = 8
    depth_limit: int = 2
    max_response_bytes: int = 1_048_576
    timeout_seconds: float = 10.0
    request_interval_seconds: float = 1.0
    max_policy_delay_seconds: float = 30.0
    retry_attempt_limit: int = 4
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 30.0
    retry_delay_budget_seconds: float = 60.0
    user_agent: str = "SaraBusinessUnderstanding/1.0"
    obey_robots: bool = True

    def validate(self) -> None:
        if self.page_limit < 1 or self.page_limit > 50:
            raise ValueError("page_limit must be between 1 and 50")
        if self.depth_limit < 0 or self.depth_limit > 5:
            raise ValueError("depth_limit must be between 0 and 5")
        if self.max_response_bytes < 16_384 or self.max_response_bytes > 10_485_760:
            raise ValueError("max_response_bytes must be between 16384 and 10485760")
        if (
            not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 120
        ):
            raise ValueError("timeout_seconds must be finite, greater than zero and at most 120")
        if (
            not math.isfinite(self.request_interval_seconds)
            or self.request_interval_seconds <= 0
            or self.request_interval_seconds > 60
        ):
            raise ValueError(
                "request_interval_seconds must be finite, greater than zero and at most 60"
            )
        if (
            not math.isfinite(self.max_policy_delay_seconds)
            or self.max_policy_delay_seconds < self.request_interval_seconds
            or self.max_policy_delay_seconds > 300
        ):
            raise ValueError(
                "max_policy_delay_seconds must be finite, at least request_interval_seconds and at most 300"
            )
        if (
            isinstance(self.retry_attempt_limit, bool)
            or not isinstance(self.retry_attempt_limit, int)
            or self.retry_attempt_limit < 1
            or self.retry_attempt_limit > 8
        ):
            raise ValueError("retry_attempt_limit must be an integer between 1 and 8")
        if (
            not math.isfinite(self.retry_base_delay_seconds)
            or self.retry_base_delay_seconds <= 0
            or self.retry_base_delay_seconds > 60
        ):
            raise ValueError(
                "retry_base_delay_seconds must be finite, greater than zero and at most 60"
            )
        if (
            not math.isfinite(self.retry_max_delay_seconds)
            or self.retry_max_delay_seconds < self.retry_base_delay_seconds
            or self.retry_max_delay_seconds > 300
        ):
            raise ValueError(
                "retry_max_delay_seconds must be finite, at least retry_base_delay_seconds and at most 300"
            )
        if (
            not math.isfinite(self.retry_delay_budget_seconds)
            or self.retry_delay_budget_seconds < self.retry_max_delay_seconds
            or self.retry_delay_budget_seconds > 900
        ):
            raise ValueError(
                "retry_delay_budget_seconds must be finite, at least retry_max_delay_seconds and at most 900"
            )
        if not self.user_agent.strip():
            raise ValueError("user_agent must not be blank")
        if not self.obey_robots:
            raise ValueError("Phase-6 website acquisition requires robots/source-policy compliance")


@dataclass(frozen=True)
class PageCapture:
    requested_url: str
    final_url: str
    depth: int
    retrieved_at: str
    status: int
    media_type: str
    charset: str
    headers: dict[str, str]
    body: bytes
    content_sha256: str
    artifact_ref: str
    parsed: ParsedPage


@dataclass(frozen=True)
class CrawlResult:
    captures: tuple[PageCapture, ...]
    errors: tuple[str, ...]
    canonical_home_url: str | None
    frontier_exhausted: bool


@dataclass(frozen=True)
class WebsiteAcquisitionStats:
    session_id: str
    business_entity_id: str
    start_url: str
    canonical_home_url: str | None
    status: str
    pages_fetched: int
    evidence_items_created: int
    observations_created: int
    channels_created: int
    channels_refreshed: int
    facts_created: int
    facts_replaced: int
    fact_support_links_created: int
    not_observed_facts_created: int
    fetch_errors: tuple[str, ...]
    unresolved_predicates: tuple[str, ...]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def opaque_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join((ID_NAMESPACE, *(str(part) for part in parts)))
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"
