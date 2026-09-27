from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .model import (
    REVIEW_ARRAY_FIELDS,
    ParsedReview,
    ReviewIntelligenceError,
    canonical_json,
    sha256_text,
)


def _text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewIntelligenceError(f"review field {field!r} must be text when present")
    value = value.strip()
    return value or None


def _first_text(review: dict[str, Any], *fields: str) -> str | None:
    for field in fields:
        value = _text(review.get(field), field=field)
        if value is not None:
            return value
    return None


def _number(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewIntelligenceError(f"review field {field!r} must be numeric when present")
    result = float(value)
    if not math.isfinite(result):
        raise ReviewIntelligenceError(f"review field {field!r} must be finite")
    return result


def _integer(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewIntelligenceError(f"review field {field!r} must be an integer when present")
    return int(value)


def _iso_timestamp(value: object, *, field: str) -> str | None:
    text = _text(value, field=field)
    if text is None:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReviewIntelligenceError(
            f"review field {field!r} is not a valid ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewIntelligenceError(f"review field {field!r} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _micros_timestamp(value: object, *, field: str) -> str | None:
    micros = _integer(value, field=field)
    if micros in (None, 0):
        return None
    if micros < 0:
        raise ReviewIntelligenceError(f"review field {field!r} must not be negative")
    try:
        return datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise ReviewIntelligenceError(f"review field {field!r} is outside timestamp range") from exc


def _published_at(review: dict[str, Any]) -> str | None:
    explicit = _iso_timestamp(review.get("published_at"), field="published_at")
    if explicit is not None:
        return explicit
    return _micros_timestamp(
        review.get("posted_at_unix_micros"), field="posted_at_unix_micros"
    )


def _updated_at(review: dict[str, Any]) -> str | None:
    return _micros_timestamp(
        review.get("updated_at_unix_micros"), field="updated_at_unix_micros"
    )


def _reply_published_at(review: dict[str, Any]) -> str | None:
    return _micros_timestamp(
        review.get("reply_posted_at_unix_micros"), field="reply_posted_at_unix_micros"
    )


def _reply_updated_at(review: dict[str, Any]) -> str | None:
    return _micros_timestamp(
        review.get("reply_updated_at_unix_micros"), field="reply_updated_at_unix_micros"
    )


def _normalized_value(review: dict[str, Any]) -> dict[str, Any]:
    review_id = _first_text(review, "review_id", "ReviewID")
    source = _first_text(review, "source", "Source")

    rating_float = _number(review.get("rating_float"), field="rating_float")
    legacy_rating = _number(review.get("Rating"), field="Rating")
    rating = rating_float if rating_float not in (None, 0.0) else legacy_rating
    rating_scale = _integer(review.get("rating_scale"), field="rating_scale")
    if rating_scale is not None and rating_scale <= 0:
        raise ReviewIntelligenceError("review field 'rating_scale' must be greater than zero")
    if rating is not None and rating < 0:
        raise ReviewIntelligenceError("review rating must not be negative")
    if rating is not None and rating_scale is not None and rating > rating_scale:
        raise ReviewIntelligenceError("review rating exceeds its declared rating scale")

    text_original = _first_text(review, "text_original", "Description")
    text_translated = _first_text(review, "text_translated")
    language = _first_text(review, "language")
    translated_language = _first_text(review, "translated_lang")
    source_when = _first_text(review, "When")

    reply_text = _first_text(review, "reply_text_original", "reply_text")
    reply_language = _first_text(review, "reply_language")
    reply_translated_language = _first_text(review, "reply_translated_lang")
    reply_published_at = _reply_published_at(review)
    reply_updated_at = _reply_updated_at(review)
    response = None
    if any(
        value is not None
        for value in (
            reply_text,
            reply_language,
            reply_translated_language,
            reply_published_at,
            reply_updated_at,
        )
    ):
        response = {
            "text": reply_text,
            "language": reply_language,
            "translated_language": reply_translated_language,
            "published_at": reply_published_at,
            "updated_at": reply_updated_at,
        }

    result = {
        "review_id": review_id,
        "source": source,
        "rating": rating,
        "rating_scale": rating_scale,
        "text_original": text_original,
        "text_translated": text_translated,
        "language": language,
        "translated_language": translated_language,
        "published_at": _published_at(review),
        "updated_at": _updated_at(review),
        "source_when": source_when,
        "owner_response": response,
    }
    if not any(
        result[key] is not None
        for key in (
            "review_id",
            "rating",
            "text_original",
            "text_translated",
            "published_at",
            "source_when",
            "owner_response",
        )
    ):
        raise ReviewIntelligenceError(
            "review record contains no stable identity, rating, text, time, or owner response"
        )
    return result


def extract_reviews(raw: dict[str, Any]) -> tuple[tuple[ParsedReview, ...], int]:
    """Extract review records from one retained Maps business snapshot.

    Exact duplicate raw review objects repeated between ``user_reviews`` and
    ``user_reviews_extended`` are collapsed. A stable source review ID is used
    as identity when available; records without one receive a deterministic
    semantic fingerprint. Different raw variants of the same review ID remain
    separate evidence rather than being silently reconciled.
    """
    if not isinstance(raw, dict):
        raise ReviewIntelligenceError("retained Maps source snapshot is not a JSON object")

    records: dict[tuple[str, str], dict[str, Any]] = {}
    source_record_count = 0
    for array_name in REVIEW_ARRAY_FIELDS:
        value = raw.get(array_name)
        if value is None:
            continue
        if not isinstance(value, list):
            raise ReviewIntelligenceError(
                f"retained Maps field {array_name!r} must be an array when present"
            )
        for index, item in enumerate(value):
            source_record_count += 1
            if not isinstance(item, dict):
                raise ReviewIntelligenceError(
                    f"retained Maps field {array_name}[{index}] is not a review object"
                )
            normalized = _normalized_value(item)
            raw_json = canonical_json(item)
            value_json = canonical_json(normalized)
            raw_hash = sha256_text(raw_json)
            value_hash = sha256_text(value_json)
            review_id = normalized["review_id"]
            source = normalized["source"]
            identity_key = (
                f"source-id:{source or 'google_maps'}:{review_id}"
                if review_id is not None
                else f"semantic-fingerprint:{value_hash}"
            )
            key = (identity_key, raw_hash)
            path = f"{array_name}[{index}]"
            existing = records.get(key)
            if existing is None:
                records[key] = {
                    "identity_key": identity_key,
                    "raw_json": raw_json,
                    "raw_sha256": raw_hash,
                    "value_json": value_json,
                    "value_sha256": value_hash,
                    "review_id": review_id,
                    "source": source,
                    "published_at": normalized["published_at"],
                    "language": normalized["language"],
                    "source_paths": [path],
                }
            else:
                existing["source_paths"].append(path)

    parsed = tuple(
        ParsedReview(
            identity_key=str(record["identity_key"]),
            raw_json=str(record["raw_json"]),
            raw_sha256=str(record["raw_sha256"]),
            value_json=str(record["value_json"]),
            value_sha256=str(record["value_sha256"]),
            review_id=record["review_id"],
            source=record["source"],
            published_at=record["published_at"],
            language=record["language"],
            source_paths=tuple(sorted(str(path) for path in record["source_paths"])),
        )
        for _key, record in sorted(records.items(), key=lambda item: item[0])
    )
    return parsed, source_record_count
