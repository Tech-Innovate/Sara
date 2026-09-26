from pathlib import Path

merge_path = Path("src/sara/website_validation_merge_probe.py")
text = merge_path.read_text(encoding="utf-8")

start = text.index("def _create_bridge_run(")
end = text.index("\n\ndef run_controlled_merge_survival_probe", start)
helpers = '''def _create_partition_run(
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    business_id: int,
) -> tuple[str, str]:
    source = conn.execute("SELECT * FROM runs WHERE id=?", (source_run_id,)).fetchone()
    if source is None:
        raise OperationalValidationError(f"merge-probe run {source_run_id!r} does not exist")
    partition_run_id = f"validation-merge-probe-partition-{business_id}"
    if conn.execute("SELECT 1 FROM runs WHERE id=?", (partition_run_id,)).fetchone() is not None:
        raise OperationalValidationError(
            f"controlled merge-probe partition run id already exists: {partition_run_id!r}"
        )
    partition_started_at = _later_timestamp(source["finished_at"] or source["started_at"])
    partition_config_json = json.dumps(
        {
            "validation_kind": "controlled_maps_identity_partition_probe",
            "source_business_id": business_id,
            "source_run_id": source_run_id,
            "synthetic_record": True,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,raw_path,"
        "status,started_at,finished_at,exit_code,error,raw_records,accepted_records,"
        "out_of_bounds_records,unlocated_records,unidentified_records,unique_seen,new_businesses"
        ") VALUES (?,?,?,?,?,?,?,?,NULL,'complete',?,?,0,NULL,2,2,0,0,0,2,1)",
        (
            partition_run_id,
            "controlled-validation-merge-probe-partition",
            source["bbox_json"],
            source["cell_km"],
            source["depth"],
            "[]",
            "sara-controlled-merge-probe",
            partition_config_json,
            partition_started_at,
            partition_started_at,
        ),
    )
    return partition_run_id, partition_started_at


def _create_bridge_run(
    conn: sqlite3.Connection,
    *,
    previous_run_id: str,
    source_run_id: str,
    business_id: int,
) -> tuple[str, str]:
    previous = conn.execute("SELECT * FROM runs WHERE id=?", (previous_run_id,)).fetchone()
    if previous is None:
        raise OperationalValidationError(
            f"merge-probe predecessor run {previous_run_id!r} does not exist"
        )
    bridge_run_id = f"validation-merge-probe-{business_id}"
    if conn.execute("SELECT 1 FROM runs WHERE id=?", (bridge_run_id,)).fetchone() is not None:
        raise OperationalValidationError(
            f"controlled merge-probe run id already exists: {bridge_run_id!r}"
        )
    bridge_started_at = _later_timestamp(previous["finished_at"] or previous["started_at"])
    bridge_config_json = json.dumps(
        {
            "validation_kind": "controlled_maps_identity_merge_probe",
            "source_business_id": business_id,
            "source_run_id": source_run_id,
            "synthetic_partition_run_id": previous_run_id,
            "synthetic_record": True,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO runs("
        "id,area_name,bbox_json,cell_km,depth,queries_json,scraper_image,config_json,raw_path,"
        "status,started_at,finished_at,exit_code,error,raw_records,accepted_records,"
        "out_of_bounds_records,unlocated_records,unidentified_records,unique_seen,new_businesses"
        ") VALUES (?,?,?,?,?,?,?,?,NULL,'complete',?,?,0,NULL,1,1,0,0,0,1,0)",
        (
            bridge_run_id,
            "controlled-validation-merge-probe",
            previous["bbox_json"],
            previous["cell_km"],
            previous["depth"],
            "[]",
            "sara-controlled-merge-probe",
            bridge_config_json,
            bridge_started_at,
            bridge_started_at,
        ),
    )
    return bridge_run_id, bridge_started_at
'''
text = text[:start] + helpers + text[end:]

old_locals = '''    conn = connect_existing(probe_db)
    duplicate_id: int | None = None
    bridge_run_id: str | None = None
'''
new_locals = '''    conn = connect_existing(probe_db)
    duplicate_id: int | None = None
    partition_run_id: str | None = None
    bridge_run_id: str | None = None
'''
if text.count(old_locals) != 1:
    raise SystemExit("merge-probe locals anchor mismatch")
text = text.replace(old_locals, new_locals, 1)

flow_start = text.index('        source_run = conn.execute(\n')
flow_end = text.index('        _bootstrap_probe(conn)', flow_start)
new_flow = '''        conn.execute("BEGIN IMMEDIATE")
        partition_run_id, partition_started_at = _create_partition_run(
            conn,
            source_run_id=source_run_id,
            business_id=business_id,
        )
        keep_values = {field: None for field in IDENTITY_FIELDS}
        keep_values[keep[0]] = keep[1]
        conn.execute(
            "UPDATE businesses SET place_id=?,cid=?,data_id=?,raw_json=?,last_seen_at=?,last_run_id=? "
            "WHERE id=?",
            (
                keep_values["place_id"],
                keep_values["cid"],
                keep_values["data_id"],
                json.dumps(keep_record, ensure_ascii=False, sort_keys=True),
                partition_started_at,
                partition_run_id,
                business_id,
            ),
        )
        conn.execute(
            "INSERT INTO run_businesses(run_id,business_id,first_observed_at) VALUES (?,?,?)",
            (partition_run_id, business_id, partition_started_at),
        )
        duplicate_id, created = upsert_business(
            conn,
            partition_run_id,
            moved_record,
            run_started_at=partition_started_at,
        )
        if not created or duplicate_id == business_id:
            raise OperationalValidationError(
                "controlled identifier partition did not create a distinct Maps row"
            )
        conn.execute(
            "INSERT INTO run_businesses(run_id,business_id,first_observed_at) VALUES (?,?,?)",
            (partition_run_id, duplicate_id, partition_started_at),
        )
        conn.commit()

'''
text = text[:flow_start] + new_flow + text[flow_end:]

old_bridge_call = '''        bridge_run_id, bridge_started_at = _create_bridge_run(
            conn,
            source_run_id=source_run_id,
            business_id=business_id,
        )
'''
new_bridge_call = '''        bridge_run_id, bridge_started_at = _create_bridge_run(
            conn,
            previous_run_id=partition_run_id,
            source_run_id=source_run_id,
            business_id=business_id,
        )
'''
if text.count(old_bridge_call) != 1:
    raise SystemExit("bridge call anchor mismatch")
text = text.replace(old_bridge_call, new_bridge_call, 1)

old_detail = '                "synthetic_duplicate_business_id": duplicate_id,\n                "synthetic_bridge_run_id": bridge_run_id,\n'
new_detail = '                "synthetic_duplicate_business_id": duplicate_id,\n                "synthetic_partition_run_id": partition_run_id,\n                "synthetic_bridge_run_id": bridge_run_id,\n'
if text.count(old_detail) != 2:
    raise SystemExit(f"result detail anchor count={text.count(old_detail)}")
text = text.replace(old_detail, new_detail)
merge_path.write_text(text, encoding="utf-8")

# Tests
test_path = Path("tests/test_website_validation_controlled_merge_probe.py")
t = test_path.read_text(encoding="utf-8")
old_asserts = '''    assert result.details["synthetic_duplicate_business_id"] != 1
    assert result.details["synthetic_bridge_run_id"] == "validation-merge-probe-1"
'''
new_asserts = '''    assert result.details["synthetic_duplicate_business_id"] != 1
    assert result.details["synthetic_partition_run_id"] == "validation-merge-probe-partition-1"
    assert result.details["synthetic_bridge_run_id"] == "validation-merge-probe-1"
'''
if t.count(old_asserts) != 1:
    raise SystemExit("test result assertions anchor mismatch")
t = t.replace(old_asserts, new_asserts, 1)

block_start = t.index('    check = sqlite3.connect(probe)\n')
block_end = t.index('\n\ndef test_controlled_merge_probe_requires_two_real_strong_identifiers', block_start)
new_block = '''    check = sqlite3.connect(probe)
    check.row_factory = sqlite3.Row
    partition = check.execute(
        "SELECT area_name,queries_json,scraper_image,config_json,raw_path,raw_records,"
        "accepted_records,unique_seen,new_businesses "
        "FROM runs WHERE id='validation-merge-probe-partition-1'"
    ).fetchone()
    assert partition is not None
    assert partition["area_name"] == "controlled-validation-merge-probe-partition"
    assert partition["queries_json"] == "[]"
    assert partition["scraper_image"] == "sara-controlled-merge-probe"
    assert partition["raw_path"] is None
    assert tuple(
        partition[field]
        for field in ("raw_records", "accepted_records", "unique_seen", "new_businesses")
    ) == (2, 2, 2, 1)
    assert json.loads(partition["config_json"]) == {
        "validation_kind": "controlled_maps_identity_partition_probe",
        "source_business_id": 1,
        "source_run_id": "r1",
        "synthetic_record": True,
    }

    bridge = check.execute(
        "SELECT area_name,queries_json,scraper_image,config_json,raw_path "
        "FROM runs WHERE id='validation-merge-probe-1'"
    ).fetchone()
    assert bridge is not None
    assert bridge["area_name"] == "controlled-validation-merge-probe"
    assert bridge["queries_json"] == "[]"
    assert bridge["scraper_image"] == "sara-controlled-merge-probe"
    assert bridge["raw_path"] is None
    assert json.loads(bridge["config_json"]) == {
        "validation_kind": "controlled_maps_identity_merge_probe",
        "source_business_id": 1,
        "source_run_id": "r1",
        "synthetic_partition_run_id": "validation-merge-probe-partition-1",
        "synthetic_record": True,
    }

    for synthetic_run_id in (
        "validation-merge-probe-partition-1",
        "validation-merge-probe-1",
    ):
        artifact_refs = {
            row[0]
            for row in check.execute(
                "SELECT e.artifact_ref FROM evidence_items e "
                "JOIN acquisition_sessions a ON a.id=e.acquisition_session_id "
                "WHERE a.legacy_run_id=?",
                (synthetic_run_id,),
            )
        }
        assert artifact_refs == {None}

    # The real acquisition run remains historical provenance for the original row only;
    # the validation-generated duplicate is never inserted into it.
    real_run_members = {
        int(row[0])
        for row in check.execute(
            "SELECT business_id FROM run_businesses WHERE run_id='r1' ORDER BY business_id"
        )
    }
    assert real_run_members == {1}
    real_run_session_count = int(
        check.execute(
            "SELECT COUNT(*) FROM acquisition_sessions WHERE legacy_run_id='r1'"
        ).fetchone()[0]
    )
    assert real_run_session_count == 0
    check.close()
'''
t = t[:block_start] + new_block + t[block_end:]
test_path.write_text(t, encoding="utf-8")

# Runbook
doc_path = Path("docs/website-operational-validation.md")
d = doc_path.read_text(encoding="utf-8")
old_para = "The merge-probe business is not asserted to be a natural duplicate. Only in `merge-probe.sqlite`, the harness partitions that one real business's strong identifiers into two temporary complementary Maps rows before Understanding bootstrap. It then creates separate Understanding anchors/evidence for those rows, reconnects them with the combined source record through Sara's normal `upsert_business` path under a later validation-generated Maps run, and runs the normal Maps synchronizer. The validation-generated run is explicitly marked synthetic, has no retained raw artifact reference, and is never represented as a real scraper execution. The source database and `validation.sqlite` are never mutated by this synthetic setup."
new_para = "The merge-probe business is not asserted to be a natural duplicate. Only in `merge-probe.sqlite`, the harness partitions that one real business's strong identifiers into two temporary complementary Maps rows under an explicitly synthetic partition run before Understanding bootstrap. That partition run has no retained raw artifact reference, so validation-generated JSON is never attributed to the original Maps artifact. The harness then creates separate Understanding anchors/evidence for those synthetic partition rows, reconnects them with the combined source record through Sara's normal `upsert_business` path under a later synthetic bridge run, and runs the normal Maps synchronizer. Both validation-generated runs are explicitly marked synthetic and have `raw_path=NULL`; neither is represented as a real scraper execution. The original source run remains untouched historical provenance, and the source database and `validation.sqlite` are never mutated by this synthetic setup."
if d.count(old_para) != 1:
    raise SystemExit("runbook SR-01 paragraph anchor mismatch")
d = d.replace(old_para, new_para, 1)
old_report = "The controlled probe records `probe_kind=controlled_complementary_identifier_partition`, the real source business ID, the temporary synthetic duplicate ID, and the synthetic bridge run ID. Those fields describe the validation setup; they are not evidence that the duplicate Maps rows or bridge run existed naturally in production."
new_report = "The controlled probe records `probe_kind=controlled_complementary_identifier_partition`, the real source business ID, the temporary synthetic duplicate ID, the synthetic partition run ID, and the synthetic bridge run ID. Those fields describe the validation setup; they are not evidence that the duplicate Maps rows or either synthetic run existed naturally in production."
if d.count(old_report) != 1:
    raise SystemExit("runbook report paragraph anchor mismatch")
d = d.replace(old_report, new_report, 1)
doc_path.write_text(d, encoding="utf-8")

Path(".github/apply_sr01_patch.py").unlink()
Path(".github/workflows/_apply_sr01_patch.yml").unlink()
