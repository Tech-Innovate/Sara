from pathlib import Path

from sara.migrations import apply_migrations
from sara.storage import connect
from sara.understanding_vocabulary import seed_business_understanding_vocabulary
from sara.website.model import (
    OFFICIAL_WEB_SOURCE_ID,
    RECONCILIATION_VERSION,
    canonical_json,
    sha256_text,
)
from sara.website.reconcile import reconcile_observation_group, values_equivalent


UNICODE_URL = "https://مثال.إختبار/قائمة?فرع=جدة"
ASCII_URL = (
    "https://xn--mgbh0fb.xn--kgbechtv/%D9%82%D8%A7%D8%A6%D9%85%D8%A9"
    "?%D9%81%D8%B1%D8%B9=%D8%AC%D8%AF%D8%A9"
)
CREATED = "2026-09-27T05:00:00+00:00"
OBSERVED = "2026-09-27T06:00:00+00:00"


def test_unicode_ascii_url_equivalence_is_versioned() -> None:
    assert RECONCILIATION_VERSION == "official-web-v4"
    assert values_equivalent(
        "business.website.official", canonical_json(UNICODE_URL), canonical_json(ASCII_URL)
    )
    assert not values_equivalent(
        "business.website.official",
        canonical_json(UNICODE_URL),
        canonical_json("https://other.example/"),
    )


def _prepared(path: Path):
    conn = connect(path)
    assert apply_migrations(conn) == (1, 2)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) "
        "VALUES ('be','business_entity',?,?)",
        (CREATED, CREATED),
    )
    conn.execute(
        "INSERT INTO business_entities(id,display_name,entity_type,lifecycle_status,created_at,updated_at) "
        "VALUES ('be','Unicode Business','unknown','unknown',?,?)",
        (CREATED, CREATED),
    )
    for source_id, source_type in (
        ("src_google_maps", "google_maps"),
        (OFFICIAL_WEB_SOURCE_ID, "official_web"),
    ):
        conn.execute(
            "INSERT INTO sources(id,source_type,name,created_at,active) VALUES (?,?,?,?,1)",
            (source_id, source_type, source_id, CREATED),
        )

    old_json = canonical_json(UNICODE_URL)
    old_hash = sha256_text(old_json)
    config_hash = sha256_text("{}")
    conn.execute(
        "INSERT INTO acquisition_sessions(id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,evidence_count,observation_count) "
        "VALUES ('maps','be','src_google_maps','sara.maps_backfill','2','{}',?,'complete',?,?,1,1)",
        (config_hash, CREATED, CREATED),
    )
    conn.execute(
        "INSERT INTO evidence_items(id,acquisition_session_id,source_id,source_role,status,retrieved_at,"
        "metadata_json,created_at) VALUES ('ev_maps','maps','src_google_maps','platform','usable',?,'{}',?)",
        (CREATED, CREATED),
    )
    conn.execute(
        "INSERT INTO observations(id,subject_id,predicate,evidence_id,value_json,normalized_value_json,"
        "value_hash,observation_kind,observed_at,extracted_at,extraction_method,extractor_name,"
        "extractor_version,confidence,created_at) VALUES ("
        "'obs_maps','be','business.website.official','ev_maps',?,?,?,'structured_value',?,?,"
        "'deterministic_parser','sara.maps_backfill','2',1.0,?)",
        (old_json, old_json, old_hash, CREATED, CREATED, CREATED),
    )
    conn.execute(
        "INSERT INTO facts(id,subject_id,predicate,fact_slot,value_json,normalized_value_json,value_hash,"
        "status,valid_from,last_verified_at,reconciled_at,reconciliation_version,created_at) VALUES ("
        "'fact_maps','be','business.website.official','__single__',?,?,?,'single_source',?,?,?,?,?)",
        (old_json, old_json, old_hash, CREATED, CREATED, CREATED, "maps-backfill-v2", CREATED),
    )
    conn.execute(
        "INSERT INTO fact_observation_support(fact_id,observation_id,support_role) "
        "VALUES ('fact_maps','obs_maps','supports')"
    )

    new_json = canonical_json(ASCII_URL)
    new_hash = sha256_text(new_json)
    conn.execute(
        "INSERT INTO acquisition_sessions(id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,evidence_count,observation_count) "
        "VALUES ('web','be',?,'sara.website','4','{}',?,'complete',?,?,1,1)",
        (OFFICIAL_WEB_SOURCE_ID, config_hash, OBSERVED, OBSERVED),
    )
    conn.execute(
        "INSERT INTO evidence_items(id,acquisition_session_id,source_id,source_role,status,retrieved_at,"
        "source_locator,metadata_json,created_at) VALUES ('ev_web','web',?,'official','usable',?,?, '{}',?)",
        (OFFICIAL_WEB_SOURCE_ID, OBSERVED, ASCII_URL, OBSERVED),
    )
    conn.execute(
        "INSERT INTO observations(id,subject_id,predicate,evidence_id,value_json,normalized_value_json,"
        "value_hash,observation_kind,observed_at,extracted_at,extraction_method,extractor_name,"
        "extractor_version,confidence,created_at) VALUES ("
        "'obs_web','be','business.website.official','ev_web',?,?,?,'structured_value',?,?,"
        "'deterministic_parser','sara.website','4',1.0,?)",
        (new_json, new_json, new_hash, OBSERVED, OBSERVED, OBSERVED),
    )
    conn.commit()
    return conn, {
        "id": "obs_web",
        "evidence_id": "ev_web",
        "value_json": new_json,
        "value_hash": new_hash,
    }


def test_unicode_maps_value_is_not_promoted_as_exact_support(tmp_path: Path) -> None:
    conn, observation = _prepared(tmp_path / "unicode-reconcile.sqlite")
    created, replaced, links = reconcile_observation_group(
        conn,
        entity_id="be",
        predicate="business.website.official",
        fact_slot="__single__",
        observations=[observation],
        observed_at=OBSERVED,
        reconciled_at="2026-09-27T06:00:01+00:00",
    )
    conn.commit()
    assert (created, replaced, links) == (1, 1, 1)

    current = conn.execute(
        "SELECT id,value_json,status,reconciliation_version FROM facts "
        "WHERE subject_id='be' AND predicate='business.website.official' AND valid_to IS NULL"
    ).fetchone()
    assert current[1] == canonical_json(ASCII_URL)
    assert current[2] == "single_source"
    assert current[3] == RECONCILIATION_VERSION
    support = [
        tuple(row)
        for row in conn.execute(
            "SELECT fos.support_role,e.source_id FROM fact_observation_support fos "
            "JOIN observations o ON o.id=fos.observation_id "
            "JOIN evidence_items e ON e.id=o.evidence_id WHERE fos.fact_id=?",
            (current[0],),
        )
    ]
    assert support == [("supports", OFFICIAL_WEB_SOURCE_ID)]
    conn.close()
