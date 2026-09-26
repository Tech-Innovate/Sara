from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class MapsSourceShapeError(ValueError):
    """A Maps source record contains mutually inconsistent compatibility fields."""


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def official_website(record: Mapping[str, Any]) -> str | None:
    """Return Sara's canonical website value from supported Maps source shapes.

    Sara's pinned gosom/google-maps-scraper v1.18.1 evidence uses ``web_site``
    while later/source fixtures may use ``website``. The compatibility alias is
    resolved at the source boundary; Sara's canonical vocabulary remains
    ``website`` / ``business.website.official``.

    When both spellings are present they must agree after surrounding whitespace
    is removed. A disagreement is source-shape ambiguity and fails closed rather
    than silently selecting one value.
    """

    website = _text(record.get("website"))
    legacy_web_site = _text(record.get("web_site"))
    if website is not None and legacy_web_site is not None and website != legacy_web_site:
        raise MapsSourceShapeError(
            "conflicting Maps website fields: "
            f"website={website!r}, web_site={legacy_web_site!r}"
        )
    return website if website is not None else legacy_web_site
