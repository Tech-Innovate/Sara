from __future__ import annotations

import sqlite3
from typing import Any

from ..understanding_vocabulary import verify_review_intelligence_vocabulary
from .core import extract_retained_reviews as _extract_retained_reviews
from .core import main
from .model import ReviewExtractionStats, ReviewIntelligenceError


def extract_retained_reviews(
    conn: sqlite3.Connection,
    **kwargs: Any,
) -> ReviewExtractionStats:
    """Public extraction API with the Review Intelligence vocabulary gate."""
    verify_review_intelligence_vocabulary(conn)
    return _extract_retained_reviews(conn, **kwargs)


__all__ = [
    "ReviewExtractionStats",
    "ReviewIntelligenceError",
    "extract_retained_reviews",
    "main",
]
