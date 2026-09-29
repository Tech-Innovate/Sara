from __future__ import annotations

from sara.dossier.assessment_policy import _absence_inspection_current
from sara.dossier.integrity import additional_fact_integrity


def _not_observed_fact(*supports: dict) -> dict:
    return {
        "id": "fact_capability",
        "subject_id": "entity-a",
        "status": "not_observed",
        "freshness": {"is_stale": False},
        "observation_support": [],
        "acquisition_support": list(supports),
    }


def test_completed_subject_matched_absence_support_is_current_inspection() -> None:
    fact = _not_observed_fact(
        {
            "support_role": "supports_absence",
            "acquisition_session_id": "acq-valid",
            "status": "complete",
            "target_subject_id": "entity-a",
        }
    )
    assert _absence_inspection_current(fact) is True
    assert additional_fact_integrity([fact]) == []


def test_failed_absence_support_does_not_count_as_current_inspection() -> None:
    fact = _not_observed_fact(
        {
            "support_role": "supports_absence",
            "acquisition_session_id": "acq-failed",
            "status": "failed",
            "target_subject_id": "entity-a",
        }
    )
    assert _absence_inspection_current(fact) is False
    assert additional_fact_integrity([fact]) == [
        {
            "code": "not_observed_absence_support_not_complete",
            "fact_id": "fact_capability",
            "acquisition_session_id": "acq-failed",
            "acquisition_status": "failed",
        }
    ]


def test_wrong_subject_absence_support_does_not_count_as_current_inspection() -> None:
    fact = _not_observed_fact(
        {
            "support_role": "supports_absence",
            "acquisition_session_id": "acq-other",
            "status": "complete",
            "target_subject_id": "entity-b",
        }
    )
    assert _absence_inspection_current(fact) is False
    assert additional_fact_integrity([fact]) == [
        {
            "code": "not_observed_absence_support_subject_mismatch",
            "fact_id": "fact_capability",
            "acquisition_session_id": "acq-other",
            "fact_subject_id": "entity-a",
            "target_subject_id": "entity-b",
        }
    ]


def test_explicit_null_absence_target_is_nonqualifying_and_corrupt() -> None:
    fact = _not_observed_fact(
        {
            "support_role": "supports_absence",
            "acquisition_session_id": "acq-null-target",
            "status": "complete",
            "target_subject_id": None,
        }
    )
    assert _absence_inspection_current(fact) is False
    assert additional_fact_integrity([fact]) == [
        {
            "code": "not_observed_absence_support_subject_mismatch",
            "fact_id": "fact_capability",
            "acquisition_session_id": "acq-null-target",
            "fact_subject_id": "entity-a",
            "target_subject_id": None,
        }
    ]


def test_legacy_absence_support_without_target_column_is_nonqualifying_not_corruption() -> None:
    fact = _not_observed_fact(
        {
            "support_role": "supports_absence",
            "acquisition_session_id": "acq-legacy",
            "status": "complete",
        }
    )
    assert _absence_inspection_current(fact) is False
    assert additional_fact_integrity([fact]) == []
