from __future__ import annotations

from pathlib import Path

from sara.migrations import apply_migrations
from sara.storage import connect
from sara.understanding_vocabulary import seed_business_understanding_vocabulary
from sara.website_validation import (
    validate_fact_provenance,
    validate_multi_branch_groups,
    validate_one_to_one_maps_anchors,
)


def _prepared(tmp_path: Path):
    conn = connect(tmp_path / "validation-second-review.sqlite")
    apply_migrations(conn)
    seed_business_understanding_vocabulary(conn)
    return conn


def _insert_business_and_anchor(conn, business_id: int, entity_id: str, location_id: str) -> None:
    created = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO businesses(id,canonical_key,title,first_seen_at,last_seen_at,raw_json) "
        "VALUES (?,?,?,?,?,?)",
        (
            business_id,
            f"place:p{business_id}",
            f"Business {business_id}",
            created,
            created,
            "{}",
        ),
    )
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) VALUES (?, 'business_entity', ?, ?)",
        (entity_id, created, created),
    )
    conn.execute(
        "INSERT INTO business_entities(id,display_name,created_at,updated_at) VALUES (?,?,?,?)",
        (entity_id, entity_id, created, created),
    )
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) VALUES (?, 'location', ?, ?)",
        (location_id, created, created),
    )
    conn.execute(
        "INSERT INTO business_locations(id,business_entity_id,created_at,updated_at) VALUES (?,?,?,?)",
        (location_id, entity_id, created, created),
    )
    conn.execute(
        "INSERT INTO maps_business_location_links(business_id,location_id,linked_at) VALUES (?,?,?)",
        (business_id, location_id, created),
    )


def test_multi_branch_check_uses_canonical_entity_not_raw_owner(tmp_path: Path) -> None:
    conn = _prepared(tmp_path)
    _insert_business_and_anchor(conn, 1, "be1", "loc1")
    _insert_business_and_anchor(conn, 2, "be2", "loc2")
    conn.commit()
    assert validate_multi_branch_groups(conn, [(1, 2)]).passed is True

    merged_at = "2026-02-01T00:00:00+00:00"
    conn.execute(
        "UPDATE knowledge_subjects SET record_state='merged',merged_into_subject_id='be1',"
        "merged_at=?,updated_at=? WHERE id='be2'",
        (merged_at, merged_at),
    )
    conn.commit()

    result = validate_multi_branch_groups(conn, [(1, 2)])
    assert result.passed is False
    mapping = result.details["groups_inspected"][0]["canonical_entity_ids"]
    assert mapping == {"1": "be1", "2": "be1"}
    conn.close()


def test_anchor_check_rejects_link_that_resolves_to_another_business_location(tmp_path: Path) -> None:
    conn = _prepared(tmp_path)
    _insert_business_and_anchor(conn, 1, "be1", "loc1")
    _insert_business_and_anchor(conn, 2, "be2", "loc2")
    conn.commit()
    assert validate_one_to_one_maps_anchors(conn).passed is True

    merged_at = "2026-02-01T00:00:00+00:00"
    conn.execute(
        "UPDATE knowledge_subjects SET record_state='merged',merged_into_subject_id='loc1',"
        "merged_at=?,updated_at=? WHERE id='loc2'",
        (merged_at, merged_at),
    )
    conn.commit()

    result = validate_one_to_one_maps_anchors(conn)
    assert result.passed is False
    assert result.details["duplicate_canonical_locations"] == {"loc1": [1, 2]}
    assert any(
        failure.get("business_id") == 2
        and failure.get("error") == "Maps link points to a non-canonical location"
        for failure in result.details["failures"]
    )
    conn.close()


def test_global_fact_provenance_rejects_nonusable_support(tmp_path: Path) -> None:
    conn = _prepared(tmp_path)
    _insert_business_and_anchor(conn, 1, "be1", "loc1")
    created = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO sources(id,source_type,name,created_at) "
        "VALUES ('src_bad','official_website','Bad evidence source',?)",
        (created,),
    )
    conn.execute(
        "INSERT INTO acquisition_sessions("
        "id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,"
        "status,started_at,finished_at,evidence_count,observation_count"
        ") VALUES ('acq_bad','be1','src_bad','test','1','{}',?,'complete',?,?,1,1)",
        ("0" * 64, created, created),
    )
    conn.execute(
        "INSERT INTO evidence_items("
        "id,acquisition_session_id,source_id,source_role,status,retrieved_at,metadata_json,created_at"
        ") VALUES ('ev_bad','acq_bad','src_bad','official','incomplete',?,'{}',?)",
        (created, created),
    )
    value_hash = "1" * 64
    conn.execute(
        "INSERT INTO observations("
        "id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,"
        "observation_kind,observed_at,extracted_at,extraction_method,extractor_name,extractor_version,created_at"
        ") VALUES ('obs_bad','be1','business.name.trading','ev_bad','\"One\"','\"One\"',?,"
        "'structured_value',?,?,'deterministic_parser','test','1',?)",
        (value_hash, created, created, created),
    )
    conn.execute(
        "INSERT INTO facts("
        "id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,status,"
        "valid_from,last_verified_at,reconciled_at,reconciliation_version,created_at"
        ") VALUES ('fact_bad','be1','business.name.trading','__single__','\"One\"','\"One\"',?,"
        "'single_source',?,?,?,?,?)",
        (value_hash, created, created, created, "test-v1", created),
    )
    conn.execute(
        "INSERT INTO fact_observation_support(fact_id,observation_id,support_role) "
        "VALUES ('fact_bad','obs_bad','supports')"
    )
    conn.commit()

    result = validate_fact_provenance(conn)
    assert result.passed is False
    assert result.details["value_facts_without_usable_support"] == ["fact_bad"]
    assert result.details["value_facts_with_nonusable_support"] == ["fact_bad"]
    assert result.details["single_source_usable_source_count_mismatch"] == [
        {"fact_id": "fact_bad", "usable_source_count": 0}
    ]
    conn.close()
