"""CLI for deterministic dossier-driven acquisition planning.

Resolves the target through the canonical Understanding resolver (never
a direct table read), decides one action or stop, persists the decision
record honestly (a duplicate replay re-reads and prints the persisted
record; every other failure is an error), and prints the result.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .acquisition_planner import PLANNER_POLICY_VERSION, plan_next_acquisition
from .dossier.core import DossierQueryError, resolve_selection
from .migrations import MigrationError, apply_migrations
from .storage import connect_existing, utc_now
from .understanding_vocabulary import VocabularySeedError


def _persist(conn: sqlite3.Connection, decision, *, entity_id: str) -> dict | None:
    """Insert the decision; on duplicate-key replay return the stored row.

    Returns None on successful insert, or the previously persisted row
    for a deterministic replay. Every other IntegrityError (for example
    a foreign-key violation) propagates: the CLI must not report success
    for an unpersisted decision.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO planner_decisions("
            "id,business_entity_id,decided_at,action,stop_reason,reason_code,"
            "target_domain,policy_version,details_json,created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                decision.decision_id, entity_id,
                decision.details["decided_at"], decision.action,
                decision.stop_reason, decision.reason_code,
                decision.target_domain, decision.policy_version,
                json.dumps(decision.details, sort_keys=True, ensure_ascii=False),
                utc_now(),
            ),
        )
        conn.commit()
        return None
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        row = conn.execute(
            "SELECT id,business_entity_id,decided_at,action,stop_reason,reason_code,"
            "target_domain,policy_version,details_json FROM planner_decisions "
            "WHERE id=?", (decision.decision_id,),
        ).fetchone()
        if row is None:
            raise  # not a duplicate replay: surface the real constraint failure
        if str(exc) != "UNIQUE constraint failed: planner_decisions.id":
            raise
        return {
            "decision_id": row[0], "entity_id": row[1], "decided_at": row[2],
            "action": row[3], "stop_reason": row[4], "reason_code": row[5],
            "target_domain": row[6], "policy_version": row[7],
            "details": json.loads(row[8]),
        }
    except BaseException:
        conn.rollback()
        raise


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
    selector.add_argument("--canonical-key")
    selector.add_argument("--entity-id")
    parser.add_argument("--max-decisions", type=int, default=25)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    conn = None
    try:
        conn = connect_existing(Path(args.db))
        apply_migrations(conn)
        entity_id, resolution = resolve_selection(
            conn,
            business_id=args.business_id,
            canonical_key=args.canonical_key,
            entity_id=args.entity_id,
        )
        decisions_taken = int(conn.execute(
            "SELECT COUNT(*) FROM planner_decisions WHERE business_entity_id=? "
            "AND action IS NOT NULL", (entity_id,)).fetchone()[0])
        decision = plan_next_acquisition(
            conn, entity_id=entity_id, now=utc_now(),
            decisions_taken=decisions_taken,
            max_decisions=args.max_decisions,
        )
        replayed = _persist(conn, decision, entity_id=entity_id)
        if replayed is not None:
            payload = {
                "decision_id": replayed["decision_id"],
                "action": replayed["action"],
                "stop_reason": replayed["stop_reason"],
                "reason_code": replayed["reason_code"],
                "target_domain": replayed["target_domain"],
                "policy_version": replayed["policy_version"],
                "details": replayed["details"],
                "replayed": True,
            }
        else:
            payload = {
                "decision_id": decision.decision_id,
                "action": decision.action,
                "stop_reason": decision.stop_reason,
                "reason_code": decision.reason_code,
                "target_domain": decision.target_domain,
                "policy_version": decision.policy_version,
                "details": decision.details,
            }
        payload["entity_resolution"] = resolution
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
