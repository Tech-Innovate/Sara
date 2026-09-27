from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class ReviewIntelligenceError(RuntimeError):
    """Retained review evidence cannot be extracted safely."""


COLLECTOR_NAME = "sara.reviews.maps_snapshot"
COLLECTOR_VERSION = "1"
REVIEW_PREDICATE = "reputation.customer_review"
ID_NAMESPACE = "sara.business-understanding.maps-reviews.v1"
REVIEW_ARRAY_FIELDS = ("user_reviews", "user_reviews_extended")


@dataclass(frozen=True)
class ParsedReview:
    identity_key: str
    raw_json: str
    raw_sha256: str
    value_json: str
    value_sha256: str
    review_id: str | None
    source: str | None
    published_at: str | None
    language: str | None
    source_paths: tuple[str, ...]


@dataclass(frozen=True)
class ReviewExtractionStats:
    session_id: str
    business_id: int
    business_entity_id: str
    location_id: str
    source_evidence_id: str
    source_review_records: int
    unique_review_evidence: int
    evidence_items_created: int
    observations_created: int
    duplicate_source_records_collapsed: int
    already_extracted: bool


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def opaque_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join((ID_NAMESPACE, *(str(part) for part in parts)))
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"
