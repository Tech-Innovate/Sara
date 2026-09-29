"""CLI for deterministic dossier-driven acquisition planning.

Reads the persisted assessment, decides one action or stop, persists the
decision record, and prints it. The planner never performs the acquisition
itself: executing the chosen action is the collector CLI's job, after
which a mandatory reassessment feeds the next decision cycle.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .acquisition_planner import PLANNER_POLICY_VERSION, plan_next_acquisition
from .dossier.core import DossierQueryError
from .migrations import MigrationError, apply_migrations
from .storage import connect_existing, utc_now
from .understanding_vocabulary import VocabularySeedError


def persist_decision(conn: sqlite3.Connection, decision) -> None:
    """Insert the decision record unless its deterministic id already exists."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO planner_decisions("
            "id,business_entity_id,decided_at,action,stop_reason,reason_code,"
            "target_domain,policy_version,details_json,created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                decision.decision_id,
                _entity(conn, decision),
                decision.details["decided_at"],
                decision.action,
                decision.stop_reason,
                decision.reason_code,
                decision.target_domain,
                decision.policy_version,
                json.dumps(decision.details, sort_keys=True, ensure_ascii=False),
                utc_now(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()  # deterministic replay: same decision already recorded
    except BaseException:
        conn.rollback()
        raise


def _entity(conn: sqlite3.Connection, decision) -> str:
    return decision.details.get("entity_id", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sara-plan-acquire",
        description=(
            "Decide the single next bounded acquisition action for one "
            "business from its persisted dossier assessment."
        ),
    )
    parser.add_argument("--db", required=True)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--business-id", type=int)
    selector.add_argument("--entity-id")
    parser.add_argument("--max-decisions", type=int, default=25)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    conn = None
    try:
        conn = connect_existing(Path(args.db))
        apply_migrations(conn)
        entity_id = args.entity_id
        if entity_id is None:
            row = conn.execute(
                "SELECT bl.business_entity_id FROM maps_business_location_links m "
                "JOIN business_locations bl ON bl.id=m.location_id "
                "WHERE m.business_id=?", (args.business_id,),
            ).fetchone()
            if row is None:
                print("business has no Understanding anchor", file=sys.stderr)
                return 2
            entity_id = row[0]
        decision = plan_next_acquisition(
            conn, entity_id=entity_id, now=utc_now(),
            decisions_taken=int(conn.execute(
                "SELECT COUNT(*) FROM planner_decisions WHERE business_entity_id=? "
                "AND action IS NOT NULL", (entity_id,)).fetchone()[0]),
            max_decisions=args.max_decisions,
        )
        decision.details["entity_id"] = entity_id
        persist_decision(conn, decision)
        payload = {
            "decision_id": decision.decision_id,
            "action": decision.action,
            "stop_reason": decision.stop_reason,
            "reason_code": decision.reason_code,
            "target_domain": decision.target_domain,
            "policy_version": decision.policy_version,
            "details": decision.details,
        }
        options = {"ensure_ascii": False, "sort_keys": True}
        if args.pretty:
            print(json.dumps(payload, indent=2, **options))
        else:
            print(json.dumps(payload, separators=(",", ":"), **options))
        return 0
    except (
        FileNotFoundError, MigrationError, VocabularySeedError,
        DossierQueryError, sqlite3.Error, ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
