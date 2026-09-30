from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class ReviewIntelligenceError(RuntimeError):
    """Retained review evidence cannot be extracted safely."""


class ReviewTargetUnavailableError(ReviewIntelligenceError):
    """The requested target has no currently extractable snapshot.

    Raised for selection- and evidence-availability failures (unknown
    business, no current Maps selector or Understanding location, no
    matching retained evidence): nothing is acquirable for this target
    right now. Entity-scoped extraction skips such targets with an
    explicit reason instead of aborting; parse- and integrity-class
    failures keep raising ReviewIntelligenceError.
    """


COLLECTOR_NAME = "sara.reviews.maps_snapshot"
# v2: the frozen session config gains the derived extraction_outcome
# key. Sessions produced by v1 (identical extraction semantics,
# outcome-free config) replay through the legacy-id/legacy-config
# compatibility path in core._verify_existing instead of colliding
# as incompatible provenance or minting duplicate review evidence.
COLLECTOR_VERSION = "2"
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
    source_business_entity_id: str
    source_location_id: str
    canonical_location_id: str
    source_evidence_id: str
    source_review_records: int
    review_evidence_records: int
    evidence_items_created: int
    observations_created: int
    duplicate_source_records_collapsed: int
    already_extracted: bool
    status: str = "complete"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def opaque_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join((ID_NAMESPACE, *(str(part) for part in parts)))
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"
