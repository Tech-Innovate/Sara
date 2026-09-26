from __future__ import annotations

import sqlite3
from typing import Any

from .core import identifier_rows, location_owner, resolve_subject, row_dict


def enrich_location_aliases(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    location_rows: list[dict[str, Any]],
    current_location_ids: list[str],
) -> list[dict[str, Any]]:
    """Include historical Locations that now resolve into this entity's current Locations.

    Phase 4 intentionally keeps external-identifier ownership immutable when a duplicate
    Location redirects. Without these inbound aliases a canonical dossier could hide a
    provider identifier that still lives on the historical Location.
    """
    current = set(current_location_ids)
    existing = {str(item["id"]) for item in location_rows}

    for item in location_rows:
        owner = resolve_subject(conn, str(item["business_entity_id"]), "business_entity")
        item["original_owner_canonical_entity_id"] = str(owner["canonical"]["id"])
        if item["current_for_entity"]:
            relationship = "current"
        elif (
            str(item["canonical_location_id"]) in current
            and len(item["resolution_chain"]) > 1
        ):
            relationship = "merged_alias"
        else:
            relationship = "historical_owned"
        item["relationship_to_entity"] = relationship

    cursor = conn.execute(
        "SELECT bl.id,bl.business_entity_id,bl.label,bl.location_type,bl.created_at,bl.updated_at,"
        "ks.record_state,ks.merged_into_subject_id,ks.merged_at "
        "FROM business_locations bl JOIN knowledge_subjects ks ON ks.id=bl.id ORDER BY bl.id"
    )
    for row in cursor.fetchall():
        item = row_dict(cursor, row)
        location_id = str(item["id"])
        if location_id in existing:
            continue
        resolved = resolve_subject(conn, location_id, "location")
        canonical = resolved["canonical"]
        canonical_id = str(canonical["id"])
        if canonical_id not in current or canonical["record_state"] != "active":
            continue
        if location_owner(conn, canonical_id) != entity_id:
            continue

        original_owner = resolve_subject(
            conn, str(item["business_entity_id"]), "business_entity"
        )
        item["canonical_location_id"] = canonical_id
        item["resolution_chain"] = list(resolved["chain"])
        item["current_for_entity"] = False
        item["original_owner_canonical_entity_id"] = str(original_owner["canonical"]["id"])
        item["relationship_to_entity"] = "merged_alias"
        item["external_identifiers"] = identifier_rows(conn, location_id)
        location_rows.append(item)
        existing.add(location_id)

    location_rows.sort(key=lambda item: str(item["id"]))
    return location_rows
