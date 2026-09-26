from __future__ import annotations

import json
from pathlib import Path

from sara.migrations import apply_migrations
from sara.storage import connect
from sara.understanding_vocabulary import seed_business_understanding_vocabulary
from sara.website.model import OFFICIAL_WEB_SOURCE_ID, RECONCILIATION_VERSION, canonical_json, sha256_text
from sara.website.reconcile import reconcile_observation_group, values_equivalent
from sara.website_validation_cli import _selected_business_ids, _website_acquisition_business_ids

OLD_URL = "http://www.chennaidarbar.com/"
NEW_URL = "https://chennaidarbar.com/"
CREATED = "2026-09-26T10:00:00+00:00"
OBSERVED = "2026-09-26T11:00:00+00:00"


def _setup(path: Path):
    conn = connect(path)
    assert apply_migrations(conn) == (1,)
    seed_business_understanding_vocabulary(conn)
    conn.execute(
        "INSERT INTO knowledge_subjects(id,kind,created_at,updated_at) VALUES ('be','business_entity',?,?)",
        (CREATED, CREATED),
    )
    conn.execute(
        "INSERT INTO business_entities(id,display_name,entity_type,lifecycle_status,created_at,updated_at) "
        "VALUES ('be','Chennai Darbar','unknown','unknown',?,?)",
        (CREATED, CREATED),
    )
    for source_id, source_type in (
        ("src_google_maps", "google_maps"),
        (OFFICIAL_WEB_SOURCE_ID, "official_web"),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO sources(id,source_type,name,created_at,active) VALUES (?,?,?,?,1)",
            (source_id, source_type, source_id, CREATED),
        )

    old_json = canonical_json(OLD_URL)
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
    conn.commit()
    return conn


def _official_observation(
    conn,
    *,
    start_url: str,
    final_url: str,
    home_page: bool = True,
    session_start_url: str | None = None,
):
    value_json = canonical_json(final_url)
    value_hash = sha256_text(value_json)
    config_json = canonical_json(
        {"entity_id": "be", "start_url": session_start_url or start_url}
    )
    config_hash = sha256_text(config_json)
    metadata = canonical_json(
        {
            "acquisition_kind": "bounded_official_website",
            "entity_id": "be",
            "start_url": start_url,
            "requested_url": start_url,
            "final_url": final_url,
            "home_page": home_page,
        }
    )
    conn.execute(
        "INSERT INTO acquisition_sessions(id,target_subject_id,source_id,collector_name,collector_version,"
        "config_json,config_hash,status,started_at,finished_at,evidence_count,observation_count) "
        "VALUES ('web','be',?,'sara.website','3',?,?, 'complete',?,?,1,1)",
        (OFFICIAL_WEB_SOURCE_ID, config_json, config_hash, OBSERVED, OBSERVED),
    )
    conn.execute(
        "INSERT INTO evidence_items(id,acquisition_session_id,source_id,source_role,status,retrieved_at,"
        "source_locator,metadata_json,created_at) VALUES ('ev_web','web',?,'official','usable',?,?,?,?)",
        (OFFICIAL_WEB_SOURCE_ID, OBSERVED, final_url, metadata, OBSERVED),
    )
    conn.execute(
        "INSERT INTO observations(id,subject_id,predicate,evidence_id,value_json,normalized_value_json,"
        "value_hash,observation_kind,observed_at,extracted_at,extraction_method,extractor_name,"
        "extractor_version,confidence,created_at) VALUES ("
        "'obs_web','be','business.website.official','ev_web',?,?,?,'structured_value',?,?,"
        "'deterministic_parser','sara.website','3',1.0,?)",
        (value_json, value_json, value_hash, OBSERVED, OBSERVED, OBSERVED),
    )
    conn.commit()
    return {"id": "obs_web", "evidence_id": "ev_web", "value_json": value_json, "value_hash": value_hash}


def _reconcile(conn, observation):
    result = reconcile_observation_group(
        conn,
        entity_id="be",
        predicate="business.website.official",
        fact_slot="__single__",
        observations=[observation],
        observed_at=OBSERVED,
        reconciled_at="2026-09-26T11:00:01+00:00",
    )
    conn.commit()
    return result


def _current_links(conn):
    fact_id = conn.execute(
        "SELECT id FROM facts WHERE subject_id='be' AND predicate='business.website.official' "
        "AND valid_to IS NULL"
    ).fetchone()[0]
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT fos.support_role,e.source_id FROM fact_observation_support fos "
            "JOIN observations o ON o.id=fos.observation_id "
            "JOIN evidence_items e ON e.id=o.evidence_id WHERE fos.fact_id=? "
            "ORDER BY fos.support_role,e.source_id",
            (fact_id,),
        )
    ]


def test_verified_redirect_variant_is_not_a_contradiction(tmp_path: Path) -> None:
    conn = _setup(tmp_path / "redirect.sqlite")
    observation = _official_observation(conn, start_url=OLD_URL, final_url=NEW_URL)

    assert values_equivalent(
        "business.website.official", canonical_json(OLD_URL), canonical_json(NEW_URL)
    ) is False
    assert _reconcile(conn, observation) == (1, 1, 1)

    current = conn.execute(
        "SELECT value_json,status,reconciliation_version FROM facts "
        "WHERE subject_id='be' AND predicate='business.website.official' AND valid_to IS NULL"
    ).fetchone()
    assert json.loads(current[0]) == NEW_URL
    assert current[1] == "single_source"
    assert current[2] == "official-web-v2" == RECONCILIATION_VERSION
    assert _current_links(conn) == [("supports", OFFICIAL_WEB_SOURCE_ID)]
    conn.close()


def test_url_variant_without_matching_home_evidence_still_contradicts(tmp_path: Path) -> None:
    conn = _setup(tmp_path / "unverified.sqlite")
    observation = _official_observation(
        conn,
        start_url="http://different-start.example/",
        final_url=NEW_URL,
    )
    assert _reconcile(conn, observation) == (1, 1, 2)
    assert _current_links(conn) == [
        ("contradicts", "src_google_maps"),
        ("supports", OFFICIAL_WEB_SOURCE_ID),
    ]
    conn.close()


def test_session_start_mismatch_cannot_suppress_contradiction(tmp_path: Path) -> None:
    conn = _setup(tmp_path / "session-mismatch.sqlite")
    observation = _official_observation(
        conn,
        start_url=OLD_URL,
        final_url=NEW_URL,
        session_start_url="http://different-start.example/",
    )
    assert _reconcile(conn, observation) == (1, 1, 2)
    assert _current_links(conn) == [
        ("contradicts", "src_google_maps"),
        ("supports", OFFICIAL_WEB_SOURCE_ID),
    ]
    conn.close()


def test_cross_site_final_url_never_qualifies_as_verified_alias(tmp_path: Path) -> None:
    conn = _setup(tmp_path / "cross-site.sqlite")
    observation = _official_observation(
        conn,
        start_url=OLD_URL,
        final_url="https://other.example/",
    )
    assert _reconcile(conn, observation) == (1, 1, 2)
    assert _current_links(conn) == [
        ("contradicts", "src_google_maps"),
        ("supports", OFFICIAL_WEB_SOURCE_ID),
    ]
    conn.close()


def test_validation_acquisition_targets_exclude_multi_branch_identity_samples() -> None:
    assert _website_acquisition_business_ids([176, 8, 176]) == [8, 176]
    assert _selected_business_ids([176, 8], [(40, 127)]) == [8, 40, 127, 176]
