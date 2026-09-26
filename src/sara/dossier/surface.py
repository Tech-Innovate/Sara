from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from ..storage import connect_readonly
from ..understanding_vocabulary import DOSSIER_POLICY_VERSION
from .core import (
    DossierQueryError,
    current_facts,
    entity_record,
    evaluation_time,
    locations,
    maps_businesses,
    resolve_selection,
    verify_schema,
)
from .identity import enrich_location_aliases
from .integrity import (
    additional_assessment_integrity,
    additional_fact_integrity,
    sort_integrity_issues,
)
from .provenance import attach_provenance, controlled_unknowns
from .status import persisted_assessment, preview_domains


def build_business_dossier(
    conn: sqlite3.Connection,
    *,
    business_id: int | None = None,
    canonical_key: str | None = None,
    entity_id: str | None = None,
    evaluated_at: str | None = None,
) -> dict[str, Any]:
    if sum(value is not None for value in (business_id, canonical_key, entity_id)) != 1:
        raise DossierQueryError("select exactly one of business_id, canonical_key, or entity_id")
    if business_id is not None and business_id <= 0:
        raise DossierQueryError("business_id must be greater than zero")
    if canonical_key is not None and not canonical_key.strip():
        raise DossierQueryError("canonical_key must not be blank")
    if entity_id is not None and not entity_id.strip():
        raise DossierQueryError("entity_id must not be blank")

    verify_schema(conn)
    evaluation = evaluation_time(evaluated_at)
    canonical_entity_id, selection = resolve_selection(
        conn,
        business_id=business_id,
        canonical_key=canonical_key.strip() if canonical_key is not None else None,
        entity_id=entity_id.strip() if entity_id is not None else None,
    )
    entity = entity_record(conn, canonical_entity_id)
    location_rows, current_location_ids = locations(conn, canonical_entity_id)
    location_rows = enrich_location_aliases(
        conn,
        entity_id=canonical_entity_id,
        location_rows=location_rows,
        current_location_ids=current_location_ids,
    )
    facts = current_facts(conn, canonical_entity_id, current_location_ids, evaluation)
    evidence, integrity_issues = attach_provenance(conn, facts)
    integrity_issues = sort_integrity_issues(
        [*integrity_issues, *additional_fact_integrity(facts)]
    )
    unknowns = controlled_unknowns(conn, canonical_entity_id, current_location_ids, facts)
    persisted = persisted_assessment(conn, canonical_entity_id)
    if persisted is not None:
        persisted["integrity_issues"] = sort_integrity_issues(
            [
                *persisted["integrity_issues"],
                *additional_assessment_integrity(persisted),
            ]
        )

    return {
        "schema": "sara-business-dossier-v1",
        "fact_scope": "current_only",
        "evidence_scope": "current_fact_provenance",
        "unknown_scope": "controlled_active_predicates_on_current_subjects",
        "evaluated_at": evaluation.isoformat(),
        "selection": selection,
        "business_entity": entity,
        "maps_businesses": maps_businesses(conn, current_location_ids),
        "locations": location_rows,
        "facts": facts,
        "evidence": evidence,
        "unknowns": unknowns,
        "integrity_issues": integrity_issues,
        "dossier_status": {
            "active_policy_version": DOSSIER_POLICY_VERSION,
            "persisted_current_policy": persisted,
            "persisted_assessment_semantics": (
                "immutable_snapshot_only; phase5 does not claim it is current after later fact changes"
            ),
            "read_only_preview": {
                "derivation_version": "phase5-readonly-v1",
                "analysis_ready": False,
                "promotion_policy": "never_promote_from_preview",
                "domains": preview_domains(facts, unknowns, integrity_issues),
            },
        },
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sara-dossier",
        description="Read a Business Understanding dossier without mutating Sara state.",
    )
    parser.add_argument("--db", default="data/sara.db", help="SQLite database path")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--business-id", type=_positive_int)
    selector.add_argument("--canonical-key")
    selector.add_argument("--entity-id")
    parser.add_argument(
        "--evaluated-at",
        help="timezone-aware ISO-8601 time used only for freshness evaluation",
    )
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect_readonly(Path(args.db))
        conn.execute("BEGIN")
        dossier = build_business_dossier(
            conn,
            business_id=args.business_id,
            canonical_key=args.canonical_key,
            entity_id=args.entity_id,
            evaluated_at=args.evaluated_at,
        )
        kwargs = {"ensure_ascii": False, "sort_keys": True}
        if args.pretty:
            print(json.dumps(dossier, indent=2, **kwargs))
        else:
            print(json.dumps(dossier, separators=(",", ":"), **kwargs))
        return 0
    except (FileNotFoundError, DossierQueryError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("dossier query interrupted", file=sys.stderr)
        return 130
    except sqlite3.Error as exc:
        print(f"dossier query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
