from pathlib import Path

storage = Path("src/sara/storage.py")
text = storage.read_text(encoding="utf-8")
old_import = "from .maps_source import official_website\n"
new_import = "from .maps_source import MapsSourceShapeError, official_website\n"
if text.count(old_import) != 1:
    raise SystemExit(f"storage import match count={text.count(old_import)}")
text = text.replace(old_import, new_import, 1)

old_identity = '''def _identity(record: dict[str, Any]) -> tuple[str | None, str | None, str | None, str]:
    website = official_website(record)
    place_id = _text(record.get("place_id"))
    cid = _text(record.get("cid"))
    data_id = _text(record.get("data_id"))
    if place_id:
        return place_id, cid, data_id, f"place:{place_id}"
    if cid:
        return place_id, cid, data_id, f"cid:{cid}"
    if data_id:
        return place_id, cid, data_id, f"data:{data_id}"

    latitude, longitude = _coordinates(record)
    values = (
        _text(record.get("link")),
        _text(record.get("title")),
        _text(record.get("address")),
        _text(record.get("phone")),
        website,
        None if latitude is None else str(latitude),
        None if longitude is None else str(longitude),
    )
    normalized = [value.strip().lower() if value else "" for value in values]
    if not any(normalized):
        raise UnidentifiableRecord("record has no usable identity fields")
    digest = hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest()
    return place_id, cid, data_id, f"fallback:{digest}"
'''
new_identity = '''def _fallback_identity_key(record: dict[str, Any], website: str | None) -> str:
    latitude, longitude = _coordinates(record)
    values = (
        _text(record.get("link")),
        _text(record.get("title")),
        _text(record.get("address")),
        _text(record.get("phone")),
        website,
        None if latitude is None else str(latitude),
        None if longitude is None else str(longitude),
    )
    normalized = [value.strip().lower() if value else "" for value in values]
    if not any(normalized):
        raise UnidentifiableRecord("record has no usable identity fields")
    digest = hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest()
    return f"fallback:{digest}"


def _identity(record: dict[str, Any]) -> tuple[str | None, str | None, str | None, str]:
    website = official_website(record)
    place_id = _text(record.get("place_id"))
    cid = _text(record.get("cid"))
    data_id = _text(record.get("data_id"))
    if place_id:
        return place_id, cid, data_id, f"place:{place_id}"
    if cid:
        return place_id, cid, data_id, f"cid:{cid}"
    if data_id:
        return place_id, cid, data_id, f"data:{data_id}"
    return place_id, cid, data_id, _fallback_identity_key(record, website)
'''
if text.count(old_identity) != 1:
    raise SystemExit(f"storage identity match count={text.count(old_identity)}")
text = text.replace(old_identity, new_identity, 1)

marker = 'def _merge_matches(conn: sqlite3.Connection, matches: list[sqlite3.Row]) -> sqlite3.Row | None:\n'
alias_helper = '''def _legacy_fallback_alias_match(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    canonical_key: str,
) -> sqlite3.Row | None:
    website = official_website(record)
    if website is None:
        return None
    legacy_key = _fallback_identity_key(record, None)
    if legacy_key == canonical_key:
        return None
    row = conn.execute(
        "SELECT * FROM businesses WHERE canonical_key = ?", (legacy_key,)
    ).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(str(row["raw_json"]))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        historical_website = official_website(raw)
    except MapsSourceShapeError:
        return None
    if historical_website != website:
        return None
    return row


'''
if text.count(marker) != 1:
    raise SystemExit(f"storage merge marker count={text.count(marker)}")
text = text.replace(marker, alias_helper + marker, 1)

old_upsert = '''    place_id, cid, data_id, canonical_key = _identity(record)
    matches = _find_matches(conn, place_id, cid, data_id, canonical_key)
    existing = _merge_matches(conn, matches)
'''
new_upsert = '''    place_id, cid, data_id, canonical_key = _identity(record)
    matches = _find_matches(conn, place_id, cid, data_id, canonical_key)
    if not any((place_id, cid, data_id)):
        legacy_match = _legacy_fallback_alias_match(conn, record, canonical_key)
        if legacy_match is not None and all(
            int(row["id"]) != int(legacy_match["id"]) for row in matches
        ):
            matches.append(legacy_match)
            matches.sort(key=lambda row: int(row["id"]))
    existing = _merge_matches(conn, matches)
'''
if text.count(old_upsert) != 1:
    raise SystemExit(f"storage upsert match count={text.count(old_upsert)}")
text = text.replace(old_upsert, new_upsert, 1)
storage.write_text(text, encoding="utf-8")

tests = Path("tests/test_maps_source_shape.py")
t = tests.read_text(encoding="utf-8")
if "import hashlib\n" not in t:
    if t.count("import json\n") != 1:
        raise SystemExit("tests import insertion point missing")
    t = t.replace("import json\n", "import hashlib\nimport json\n", 1)

addition = '''


def _pre_alias_fallback_key(record: dict) -> str:
    longitude = record.get("longitude")
    if longitude is None:
        longitude = record.get("longtitude")
    values = (
        record.get("link"),
        record.get("title"),
        record.get("address"),
        record.get("phone"),
        record.get("website"),
        None if record.get("latitude") is None else str(float(record["latitude"])),
        None if longitude is None else str(float(longitude)),
    )
    normalized = [
        str(value).strip().lower() if value not in (None, "") else ""
        for value in values
    ]
    digest = hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest()
    return f"fallback:{digest}"


def _insert_pre_alias_weak_row(conn, record: dict, *, run_id: str = "r1") -> int:
    canonical_key = _pre_alias_fallback_key(record)
    raw_json = json.dumps(record, ensure_ascii=False, sort_keys=True)
    cursor = conn.execute(
        "INSERT INTO businesses("
        "canonical_key,title,address,latitude,longitude,phone,website,first_seen_at,last_seen_at,"
        "first_run_id,last_run_id,raw_json"
        ") VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?)",
        (
            canonical_key,
            record.get("title"),
            record.get("address"),
            record.get("latitude"),
            record.get("longitude"),
            record.get("phone"),
            "2026-09-26T00:00:00+00:00",
            "2026-09-26T00:00:00+00:00",
            run_id,
            run_id,
            raw_json,
        ),
    )
    business_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO run_businesses(run_id,business_id,first_observed_at) VALUES (?,?,?)",
        (run_id, business_id, "2026-09-26T00:00:00+00:00"),
    )
    conn.execute(
        "UPDATE runs SET status='complete',finished_at=?,exit_code=0,raw_records=1,accepted_records=1,"
        "unique_seen=1,new_businesses=1 WHERE id=?",
        ("2026-09-26T00:01:00+00:00", run_id),
    )
    conn.commit()
    return business_id


def test_storage_reuses_pre_alias_fallback_identity_when_raw_website_agrees(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "legacy-fallback.sqlite")
    _add_run(conn, "r1")
    record = _verbatim_v1181_shape()
    record.pop("place_id")
    record.pop("cid")
    business_id = _insert_pre_alias_weak_row(conn, record)
    legacy_key = _pre_alias_fallback_key(record)

    _add_run(conn, "r2", "2026-09-26T01:00:00+00:00")
    ingest_records(conn, "r2", [record], finalize_run=("complete", 0, None))

    canonical_shape = dict(record)
    canonical_shape["website"] = canonical_shape.pop("web_site")
    _add_run(conn, "r3", "2026-09-26T02:00:00+00:00")
    ingest_records(conn, "r3", [canonical_shape], finalize_run=("complete", 0, None))

    rows = list(
        conn.execute("SELECT id,canonical_key,website FROM businesses ORDER BY id")
    )
    assert len(rows) == 1
    assert int(rows[0]["id"]) == business_id
    assert rows[0]["canonical_key"] == legacy_key
    assert rows[0]["website"] == "https://example.test/"
    conn.close()


def test_storage_does_not_reuse_legacy_fallback_collision_with_different_raw_website(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "legacy-fallback-collision.sqlite")
    _add_run(conn, "r1")
    incoming = _verbatim_v1181_shape()
    incoming.pop("place_id")
    incoming.pop("cid")
    historical = dict(incoming)
    historical["web_site"] = "https://different.example/"
    old_id = _insert_pre_alias_weak_row(conn, historical)

    _add_run(conn, "r2", "2026-09-26T01:00:00+00:00")
    ingest_records(conn, "r2", [incoming], finalize_run=("complete", 0, None))

    rows = list(conn.execute("SELECT id,website FROM businesses ORDER BY id"))
    assert len(rows) == 2
    assert int(rows[0]["id"]) == old_id
    assert rows[0]["website"] is None
    assert rows[1]["website"] == "https://example.test/"
    conn.close()
'''
if "_pre_alias_fallback_key" in t:
    raise SystemExit("legacy fallback tests already present")
tests.write_text(t.rstrip() + addition + "\n", encoding="utf-8")

docs = Path("docs/website-operational-validation.md")
d = docs.read_text(encoding="utf-8")
needle = (
    "If both spellings are present and their nonblank values disagree after surrounding whitespace is removed, "
    "ingestion/extraction fails closed rather than silently choosing one value. Existing legacy `businesses` rows "
    "are not rewritten merely to populate the denormalized `website` column during Understanding bootstrap; "
    "historical `raw_json.web_site` can support the website fact while the legacy-table preservation checks remain meaningful.\n"
)
replacement = needle + (
    "\nFor weak-ID historical rows created before this compatibility adapter, Sara also recognizes the old fallback "
    "key that omitted `web_site`, but only when the retained historical `raw_json` resolves to the same official "
    "website as the incoming record. This prevents source-shape normalization from duplicating a known row without "
    "broadening weak-identity merges.\n"
)
if d.count(needle) != 1:
    raise SystemExit(f"runbook insertion point count={d.count(needle)}")
docs.write_text(d.replace(needle, replacement, 1), encoding="utf-8")

Path(".github/workflows/_fallback_identity_patch.yml").unlink()
Path(".github/apply_fallback_identity_patch.py").unlink()
