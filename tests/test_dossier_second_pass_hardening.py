from __future__ import annotations

from sara.dossier.integrity import (
    additional_assessment_integrity,
    additional_fact_integrity,
)


def _single_source_fact(*supports: dict) -> dict:
    return {
        "id": "fact_single",
        "status": "single_source",
        "observation_support": list(supports),
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
