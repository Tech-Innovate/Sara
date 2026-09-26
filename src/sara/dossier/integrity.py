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

        if fact["status"] == "not_observed" and not any(
            support["support_role"] == "supports_absence"
            for support in fact["acquisition_support"]
        ):
            issues.append(
                {
                    "code": "not_observed_without_absence_support",
                    "fact_id": fact["id"],
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
        ),
    )
