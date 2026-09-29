from __future__ import annotations

from pathlib import Path

from sara.dossier.provenance import controlled_unknowns
from sara.migrations import apply_migrations
from sara.storage import connect as storage_connect
from sara.understanding_vocabulary import seed_business_understanding_vocabulary


def test_evidence_only_review_predicate_is_not_reported_as_a_missing_fact(tmp_path: Path) -> None:
    conn = storage_connect(tmp_path / "dossier-review-compat.sqlite")
    assert apply_migrations(conn) == (1, 2, 3)
    seed_business_understanding_vocabulary(conn)

    unknowns = controlled_unknowns(
        conn,
        entity_id="be_hypothetical",
        current_location_ids=["loc_hypothetical"],
        facts=[],
    )

    assert unknowns
    assert {item["predicate"] for item in unknowns} == {
        row[0]
        for row in conn.execute(
            "SELECT name FROM predicate_definitions "
            "WHERE active=1 AND reconciliation_policy <> 'evidence_only'"
        )
    }
    assert all(item["predicate"] != "reputation.customer_review" for item in unknowns)
    conn.close()
