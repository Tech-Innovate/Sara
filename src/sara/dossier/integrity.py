from __future__ import annotations

from typing import Any


def additional_fact_integrity(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for fact in facts:
        if fact["status"] == "single_source":
            usable_sources = {
                str(support["source_id"])
                for support in fact["observation_support"]
                if support["support_role"] == "supports"
                and support["evidence_status"] == "usable"
            }
            if len(usable_sources) != 1:
                issues.append(
                    {
                        "code": "single_source_usable_source_count_mismatch",
                        "fact_id": fact["id"],
                        "usable_source_count": len(usable_sources),
                        "source_ids": sorted(usable_sources),
                    }
                )

        if fact["status"] == "not_observed":
            absence_support = [
                support
                for support in fact["acquisition_support"]
                if support["support_role"] == "supports_absence"
            ]
            if not absence_support:
                issues.append(
                    {
                        "code": "not_observed_without_absence_support",
                        "fact_id": fact["id"],
                    }
                )
            for support in absence_support:
                acquisition_session_id = support.get("acquisition_session_id")
                if "status" in support and support.get("status") != "complete":
                    issues.append(
                        {
                            "code": "not_observed_absence_support_not_complete",
                            "fact_id": fact["id"],
                            "acquisition_session_id": acquisition_session_id,
                            "acquisition_status": support.get("status"),
                        }
                    )
                fact_subject_id = fact.get("subject_id")
                target_subject_id = support.get("target_subject_id")
                if (
                    "target_subject_id" in support
                    and fact_subject_id is not None
                    and target_subject_id != fact_subject_id
                ):
                    issues.append(
                        {
                            "code": "not_observed_absence_support_subject_mismatch",
                            "fact_id": fact["id"],
                            "acquisition_session_id": acquisition_session_id,
                            "fact_subject_id": fact_subject_id,
                            "target_subject_id": target_subject_id,
                        }
                    )
    return issues


def additional_assessment_integrity(
    assessment: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if assessment is None:
        return []
    issues: list[dict[str, Any]] = []
    for domain in assessment["domains"]:
        if int(domain["fresh_fact_count"]) > int(domain["fact_count"]):
            issues.append(
                {
                    "code": "fresh_fact_count_exceeds_fact_count",
                    "domain": domain["domain"],
                    "fact_count": int(domain["fact_count"]),
                    "fresh_fact_count": int(domain["fresh_fact_count"]),
                }
            )
    return issues


def sort_integrity_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        issues,
        key=lambda item: (
            str(item.get("code", "")),
            str(item.get("fact_id", "")),
            str(item.get("domain", "")),
            str(item.get("observation_id", "")),
            str(item.get("acquisition_session_id", "")),
        ),
    )
