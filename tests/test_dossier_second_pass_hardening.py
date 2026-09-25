from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from sara.dossier import surface
from sara.dossier.integrity import (
    additional_assessment_integrity,
    additional_fact_integrity,
)


def _single_source_fact(*supports: dict) -> dict:
    return {
        "id": "fact_single",
        "status": "single_source",
        "observation_support": list(supports),
        "acquisition_support": [],
    }


def _not_observed_fact(*supports: dict) -> dict:
    return {
        "id": "fact_not_observed",
        "status": "not_observed",
        "observation_support": [],
        "acquisition_support": list(supports),
    }


def test_single_source_with_exactly_one_usable_source_is_consistent() -> None:
    fact = _single_source_fact(
        {
            "support_role": "supports",
            "source_id": "source-a",
            "evidence_status": "usable",
        }
    )
    assert additional_fact_integrity([fact]) == []


def test_single_source_with_two_distinct_usable_sources_is_flagged() -> None:
    fact = _single_source_fact(
        {
            "support_role": "supports",
            "source_id": "source-a",
            "evidence_status": "usable",
        },
        {
            "support_role": "supports",
            "source_id": "source-b",
            "evidence_status": "usable",
        },
    )
    issues = additional_fact_integrity([fact])
    assert issues == [
        {
            "code": "single_source_usable_source_count_mismatch",
            "fact_id": "fact_single",
            "usable_source_count": 2,
            "source_ids": ["source-a", "source-b"],
        }
    ]


def test_single_source_with_only_nonusable_support_is_flagged() -> None:
    fact = _single_source_fact(
        {
            "support_role": "supports",
            "source_id": "source-a",
            "evidence_status": "malformed",
        }
    )
    issues = additional_fact_integrity([fact])
    assert issues == [
        {
            "code": "single_source_usable_source_count_mismatch",
            "fact_id": "fact_single",
            "usable_source_count": 0,
            "source_ids": [],
        }
    ]


def test_multiple_support_observations_from_same_usable_source_still_count_as_single_source() -> None:
    fact = _single_source_fact(
        {
            "support_role": "supports",
            "source_id": "source-a",
            "evidence_status": "usable",
        },
        {
            "support_role": "supports",
            "source_id": "source-a",
            "evidence_status": "usable",
        },
    )
    assert additional_fact_integrity([fact]) == []


def test_not_observed_requires_absence_specific_acquisition_support() -> None:
    fact = _not_observed_fact(
        {"support_role": "searched"},
        {"support_role": "context"},
    )
    assert additional_fact_integrity([fact]) == [
        {
            "code": "not_observed_without_absence_support",
            "fact_id": "fact_not_observed",
        }
    ]


def test_not_observed_accepts_supports_absence_role() -> None:
    fact = _not_observed_fact(
        {"support_role": "searched"},
        {"support_role": "supports_absence"},
    )
    assert additional_fact_integrity([fact]) == []


def test_sealed_assessment_fresh_count_cannot_exceed_fact_count() -> None:
    assessment = {
        "domains": [
            {
                "domain": "identity",
                "fact_count": 2,
                "fresh_fact_count": 3,
            },
            {
                "domain": "locations",
                "fact_count": 1,
                "fresh_fact_count": 1,
            },
        ]
    }
    assert additional_assessment_integrity(assessment) == [
        {
            "code": "fresh_fact_count_exceeds_fact_count",
            "domain": "identity",
            "fact_count": 2,
            "fresh_fact_count": 3,
        }
    ]


def test_sealed_assessment_nonnegative_consistent_counts_are_accepted() -> None:
    assessment = {
        "domains": [
            {
                "domain": "identity",
                "fact_count": 2,
                "fresh_fact_count": 2,
            },
            {
                "domain": "locations",
                "fact_count": 1,
                "fresh_fact_count": 0,
            },
        ]
    }
    assert additional_assessment_integrity(assessment) == []


def test_build_business_dossier_surfaces_independent_integrity_findings(monkeypatch) -> None:
    fact = _single_source_fact(
        {
            "support_role": "supports",
            "source_id": "source-a",
            "evidence_status": "usable",
        },
        {
            "support_role": "supports",
            "source_id": "source-b",
            "evidence_status": "usable",
        },
    )
    assessment = {
        "id": "assessment",
        "integrity_issues": [],
        "domains": [
            {
                "domain": "identity",
                "fact_count": 1,
                "fresh_fact_count": 2,
            }
        ],
    }

    monkeypatch.setattr(surface, "verify_schema", lambda _conn: None)
    monkeypatch.setattr(
        surface,
        "evaluation_time",
        lambda _value: datetime(2026, 9, 26, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        surface,
        "resolve_selection",
        lambda _conn, **_kwargs: ("entity", {"kind": "entity_id", "value": "entity"}),
    )
    monkeypatch.setattr(surface, "entity_record", lambda _conn, _entity: {"id": "entity"})
    monkeypatch.setattr(surface, "locations", lambda _conn, _entity: ([], []))
    monkeypatch.setattr(
        surface,
        "enrich_location_aliases",
        lambda _conn, **kwargs: kwargs["location_rows"],
    )
    monkeypatch.setattr(surface, "current_facts", lambda *_args: [fact])
    monkeypatch.setattr(surface, "attach_provenance", lambda *_args: ([], []))
    monkeypatch.setattr(surface, "controlled_unknowns", lambda *_args: [])
    monkeypatch.setattr(surface, "persisted_assessment", lambda *_args: assessment)
    monkeypatch.setattr(surface, "maps_businesses", lambda *_args: [])
    monkeypatch.setattr(surface, "preview_domains", lambda *_args: [])

    conn = sqlite3.connect(":memory:")
    dossier = surface.build_business_dossier(conn, entity_id="entity")
    conn.close()

    assert dossier["integrity_issues"] == [
        {
            "code": "single_source_usable_source_count_mismatch",
            "fact_id": "fact_single",
            "usable_source_count": 2,
            "source_ids": ["source-a", "source-b"],
        }
    ]
    assert dossier["dossier_status"]["persisted_current_policy"]["integrity_issues"] == [
        {
            "code": "fresh_fact_count_exceeds_fact_count",
            "domain": "identity",
            "fact_count": 1,
            "fresh_fact_count": 2,
        }
    ]
