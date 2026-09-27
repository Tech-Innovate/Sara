from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .assessment import DossierAssessmentError, persist_dossier_assessment
from .core import DossierQueryError


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sara-dossier-assess",
        description=(
            "Compute and seal one policy-versioned Business Understanding dossier assessment "
            "from current retained state."
        ),
    )
    parser.add_argument("--db", default="data/sara.db", help="existing SQLite database path")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--business-id", type=_positive_int)
    selector.add_argument("--canonical-key")
    selector.add_argument("--entity-id")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    return parser


def _connect_existing_writable(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect_existing_writable(Path(args.db))
        result = persist_dossier_assessment(
            conn,
            business_id=args.business_id,
            canonical_key=(
                args.canonical_key.strip() if args.canonical_key is not None else None
            ),
            entity_id=args.entity_id.strip() if args.entity_id is not None else None,
        )
        payload = {
            "schema": "sara-dossier-assessment-result-v1",
            "assessment_id": result.assessment_id,
            "business_entity_id": result.business_entity_id,
            "policy_version": result.policy_version,
            "facts_as_of": result.facts_as_of,
            "computed_at": result.computed_at,
            "analysis_ready": result.analysis_ready,
            "blocking_mandatory_domains": list(result.blocking_mandatory_domains),
            "already_assessed": result.already_assessed,
            "domains": list(result.domains),
        }
        kwargs = {"ensure_ascii": False, "sort_keys": True}
        if args.pretty:
            print(json.dumps(payload, indent=2, **kwargs))
        else:
            print(json.dumps(payload, separators=(",", ":"), **kwargs))
        return 0
    except (FileNotFoundError, DossierAssessmentError, DossierQueryError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("dossier assessment interrupted", file=sys.stderr)
        return 130
    except sqlite3.Error as exc:
        print(f"dossier assessment failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
