from pathlib import Path

from sara.migrations import apply_migrations
from sara.storage import connect
from sara.understanding_vocabulary import seed_business_understanding_vocabulary
from sara.website.model import OFFICIAL_WEB_SOURCE_ID, canonical_json, sha256_text
from sara.website.reconcile import reconcile_observation_group


def setup_fact_db(path: Path, *, current_value: bool, valid_from: str):
    conn = connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) VALUES ('be','business_entity',?,?)",
        (valid_from, valid_from),
    )
    conn.execute(
        "INSERT INTO business_entities(id,display_name,entity_type,lifecycle_status,identity_confidence,created_at,updated_at) "
        "VALUES ('be','Business','unknown','unknown',NULL,?,?)",
        (valid_from, valid_from),
    )
    for source_id, source_type in (("src_prior", "google_maps"), (OFFICIAL_WEB_SOURCE_ID, "official_web")):
        conn.execute(
            "INSERT INTO sources(id,source_type,name,created_at,active) VALUES (?,?,?,?,1)",
            (source_id, source_type, source_id, valid_from),
        )
    value_json = canonical_json(current_value)
    value_hash = sha256_text(value_json)
    config_hash = sha256_text("{}")
    conn.execute(
        "INSERT INTO acquisition_sessions(id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,status,started_at,finished_at) "
        "VALUES ('acq_prior','be','src_prior','test','1','{}',?,'complete',?,?)",
        (config_hash, valid_from, valid_from),
    )
    conn.execute(
        "INSERT INTO evidence_items(id,acquisition_session_id,source_id,source_role,status,retrieved_at,metadata_json,created_at) "
        "VALUES ('ev_prior','acq_prior','src_prior','platform','usable',?,'{}',?)",
        (valid_from, valid_from),
    )
    conn.execute(
        "INSERT INTO observations(id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,observation_kind,observed_at,extracted_at,extraction_method,extractor_name,extractor_version,confidence,created_at) "
        "VALUES ('obs_prior','be','capability.online_booking','ev_prior',?,?,?,'detected_capability',?,?,'direct_structured','test','1',1.0,?)",
        (value_json, value_json, value_hash, valid_from, valid_from, valid_from),
    )
    conn.execute(
        "INSERT INTO facts(id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,status,valid_from,last_verified_at,reconciled_at,reconciliation_version,created_at) "
        "VALUES ('fact_prior','be','capability.online_booking','__single__',?,?,?,'single_source',?,?,?,?,?)",
        (value_json, value_json, value_hash, valid_from, valid_from, valid_from, "test-v1", valid_from),
    )
    conn.execute(
        "INSERT INTO fact_observation_support(fact_id,observation_id,support_role) VALUES ('fact_prior','obs_prior','supports')"
    )
    conn.commit()
    return conn


def insert_official_observation(conn, *, value: bool, observed_at: str, observation_id: str = "obs_web"):
    value_json = canonical_json(value)
    value_hash = sha256_text(value_json)
    acquisition_id = f"acq_{observation_id}"
    evidence_id = f"ev_{observation_id}"
    config_hash = sha256_text("{}")
    conn.execute(
        "INSERT INTO acquisition_sessions(id,target_subject_id,source_id,collector_name,collector_version,config_json,config_hash,status,started_at,finished_at) "
        "VALUES (?,?,?,'sara.website','1','{}',?,'complete',?,?)",
        (acquisition_id, "be", OFFICIAL_WEB_SOURCE_ID, config_hash, observed_at, observed_at),
    )
    conn.execute(
        "INSERT INTO evidence_items(id,acquisition_session_id,source_id,source_role,status,retrieved_at,metadata_json,created_at) "
        "VALUES (?,?,?,'official','usable',?,'{}',?)",
        (evidence_id, acquisition_id, OFFICIAL_WEB_SOURCE_ID, observed_at, observed_at),
    )
    conn.execute(
        "INSERT INTO observations(id,subject_id,predicate,evidence_id,value_json,normalized_value_json,value_hash,observation_kind,observed_at,extracted_at,extraction_method,extractor_name,extractor_version,confidence,created_at) "
        "VALUES (?,'be','capability.online_booking',?,?,?,?, 'detected_capability',?,?,'deterministic_parser','sara.website','1',1.0,?)",
        (observation_id, evidence_id, value_json, value_json, value_hash, observed_at, observed_at, observed_at),
    )
    conn.commit()
    return {
        "id": observation_id,
        "value_json": value_json,
        "value_hash": value_hash,
    }


def test_fresh_independent_same_value_versions_fact_and_confirms(tmp_path: Path) -> None:
    conn = setup_fact_db(
        tmp_path / "fresh.sqlite",
        current_value=True,
        valid_from="2026-09-25T10:00:00+00:00",
    )
    observation = insert_official_observation(
        conn, value=True, observed_at="2026-09-26T10:00:00+00:00"
    )
    created, replaced, links = reconcile_observation_group(
        conn,
        entity_id="be",
        predicate="capability.online_booking",
        fact_slot="__single__",
        observations=[observation],
        observed_at="2026-09-26T10:00:00+00:00",
        reconciled_at="2026-09-26T10:00:01+00:00",
    )
    conn.commit()
    assert (created, replaced) == (1, 1)
    assert links == 2
    assert conn.execute("SELECT valid_to FROM facts WHERE id='fact_prior'").fetchone()[0] == "2026-09-26T10:00:00+00:00"
    current = conn.execute(
        "SELECT status,last_verified_at FROM facts WHERE subject_id='be' AND predicate='capability.online_booking' AND valid_to IS NULL"
    ).fetchone()
    assert tuple(current) == ("confirmed", "2026-09-26T10:00:00+00:00")
    conn.close()


def test_older_contradictory_observation_is_retained_but_not_current_support(tmp_path: Path) -> None:
    conn = setup_fact_db(
        tmp_path / "older.sqlite",
        current_value=True,
        valid_from="2026-09-26T10:00:00+00:00",
    )
    observation = insert_official_observation(
        conn, value=False, observed_at="2026-09-25T10:00:00+00:00"
    )
    created, replaced, links = reconcile_observation_group(
        conn,
        entity_id="be",
        predicate="capability.online_booking",
        fact_slot="__single__",
        observations=[observation],
        observed_at="2026-09-25T10:00:00+00:00",
        reconciled_at="2026-09-26T11:00:00+00:00",
    )
    conn.commit()
    assert (created, replaced, links) == (0, 0, 0)
    assert conn.execute(
        "SELECT COUNT(*) FROM observations WHERE id='obs_web'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM fact_observation_support WHERE observation_id='obs_web'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT id FROM facts WHERE subject_id='be' AND predicate='capability.online_booking' AND valid_to IS NULL"
    ).fetchone()[0] == "fact_prior"
    conn.close()


def test_timestamp_offsets_are_compared_by_instant_not_lexical_text(tmp_path: Path) -> None:
    # 09:30+03:00 is 06:30Z. The new 07:00Z observation is later even though
    # its ISO text sorts before "09:30..." lexically.
    conn = setup_fact_db(
        tmp_path / "offset.sqlite",
        current_value=True,
        valid_from="2026-09-26T09:30:00+03:00",
    )
    observation = insert_official_observation(
        conn, value=True, observed_at="2026-09-26T07:00:00+00:00"
    )
    created, replaced, _links = reconcile_observation_group(
        conn,
        entity_id="be",
        predicate="capability.online_booking",
        fact_slot="__single__",
        observations=[observation],
        observed_at="2026-09-26T07:00:00+00:00",
        reconciled_at="2026-09-26T07:00:01+00:00",
    )
    conn.commit()
    assert (created, replaced) == (1, 1)
    assert conn.execute("SELECT valid_to FROM facts WHERE id='fact_prior'").fetchone()[0] == "2026-09-26T07:00:00+00:00"
    conn.close()
