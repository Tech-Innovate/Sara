from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence


class MigrationError(RuntimeError):
    """The database cannot be migrated safely to the requested Sara schema."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = "\n-- statement boundary --\n".join(
            statement.strip() for statement in self.statements
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    CHECK(version > 0),
    CHECK(length(checksum) = 64)
)
"""


BUSINESS_UNDERSTANDING_V1: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_subjects (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        record_state TEXT NOT NULL DEFAULT 'active',
        merged_into_subject_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        merged_at TEXT,
        FOREIGN KEY(merged_into_subject_id) REFERENCES knowledge_subjects(id),
        CHECK(kind IN ('business_entity', 'location', 'channel')),
        CHECK(record_state IN ('active', 'merged', 'retired')),
        CHECK(merged_into_subject_id IS NULL OR merged_into_subject_id <> id),
        CHECK(
            (record_state = 'merged' AND merged_into_subject_id IS NOT NULL AND merged_at IS NOT NULL)
            OR
            (record_state IN ('active', 'retired') AND merged_into_subject_id IS NULL AND merged_at IS NULL)
        )
    )
    """,
    """
    CREATE INDEX ix_knowledge_subjects_merge_target
    ON knowledge_subjects(merged_into_subject_id)
    """,
    """
    CREATE TRIGGER knowledge_subjects_kind_immutable
    BEFORE UPDATE OF kind ON knowledge_subjects
    WHEN NEW.kind <> OLD.kind
    BEGIN
        SELECT RAISE(ABORT, 'knowledge subject kind is immutable');
    END
    """,
    """
    CREATE TRIGGER knowledge_subjects_merge_target_kind_insert
    BEFORE INSERT ON knowledge_subjects
    WHEN NEW.merged_into_subject_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM knowledge_subjects target
          WHERE target.id = NEW.merged_into_subject_id AND target.kind = NEW.kind
      )
    BEGIN
        SELECT RAISE(ABORT, 'merged knowledge subjects must have the same kind');
    END
    """,
    """
    CREATE TRIGGER knowledge_subjects_merge_target_kind_update
    BEFORE UPDATE OF merged_into_subject_id ON knowledge_subjects
    WHEN NEW.merged_into_subject_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM knowledge_subjects target
          WHERE target.id = NEW.merged_into_subject_id AND target.kind = NEW.kind
      )
    BEGIN
        SELECT RAISE(ABORT, 'merged knowledge subjects must have the same kind');
    END
    """,
    """
    CREATE TRIGGER knowledge_subjects_merge_cycle_update
    BEFORE UPDATE OF merged_into_subject_id ON knowledge_subjects
    WHEN NEW.merged_into_subject_id IS NOT NULL
    BEGIN
        SELECT CASE WHEN EXISTS (
            WITH RECURSIVE chain(id, next_id) AS (
                SELECT id, merged_into_subject_id
                FROM knowledge_subjects
                WHERE id = NEW.merged_into_subject_id
                UNION
                SELECT ks.id, ks.merged_into_subject_id
                FROM knowledge_subjects ks
                JOIN chain c ON ks.id = c.next_id
                WHERE c.next_id IS NOT NULL
            )
            SELECT 1 FROM chain WHERE id = NEW.id
        ) THEN RAISE(ABORT, 'knowledge subject merge would create a cycle') END;
    END
    """,
    """
    CREATE TABLE business_entities (
        id TEXT PRIMARY KEY,
        display_name TEXT,
        entity_type TEXT NOT NULL DEFAULT 'unknown',
        lifecycle_status TEXT NOT NULL DEFAULT 'unknown',
        identity_confidence REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(id) REFERENCES knowledge_subjects(id),
        CHECK(entity_type IN (
            'unknown', 'independent_business', 'brand', 'operating_company',
            'franchise_operator', 'franchise_location_operator', 'institution',
            'professional_practice'
        )),
        CHECK(lifecycle_status IN (
            'unknown', 'operating', 'temporarily_closed',
            'permanently_closed', 'inactive'
        )),
        CHECK(identity_confidence IS NULL OR (identity_confidence >= 0.0 AND identity_confidence <= 1.0))
    )
    """,
    """
    CREATE TABLE business_locations (
        id TEXT PRIMARY KEY,
        business_entity_id TEXT NOT NULL,
        label TEXT,
        location_type TEXT NOT NULL DEFAULT 'unknown',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(id) REFERENCES knowledge_subjects(id),
        FOREIGN KEY(business_entity_id) REFERENCES business_entities(id),
        UNIQUE(id, business_entity_id),
        CHECK(location_type IN (
            'unknown', 'branch', 'headquarters', 'office', 'store',
            'restaurant', 'clinic', 'warehouse', 'service_area', 'virtual'
        ))
    )
    """,
    """
    CREATE INDEX ix_business_locations_entity
    ON business_locations(business_entity_id)
    """,
    """
    CREATE TABLE maps_business_location_links (
        business_id INTEGER PRIMARY KEY,
        location_id TEXT NOT NULL UNIQUE,
        linked_at TEXT NOT NULL,
        FOREIGN KEY(business_id) REFERENCES businesses(id) ON DELETE CASCADE,
        FOREIGN KEY(location_id) REFERENCES business_locations(id)
    )
    """,
    """
    CREATE TABLE sources (
        id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        name TEXT NOT NULL,
        base_url TEXT,
        created_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        CHECK(active IN (0, 1))
    )
    """,
    """
    CREATE TRIGGER business_entities_subject_kind_insert
    BEFORE INSERT ON business_entities
    WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_subjects
        WHERE id = NEW.id AND kind = 'business_entity'
    )
    BEGIN
        SELECT RAISE(ABORT, 'business_entities subject must have kind business_entity');
    END
    """,
    """
    CREATE TRIGGER business_entities_subject_kind_update
    BEFORE UPDATE OF id ON business_entities
    WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_subjects
        WHERE id = NEW.id AND kind = 'business_entity'
    )
    BEGIN
        SELECT RAISE(ABORT, 'business_entities subject must have kind business_entity');
    END
    """,
    """
    CREATE TRIGGER business_locations_subject_kind_insert
    BEFORE INSERT ON business_locations
    WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_subjects
        WHERE id = NEW.id AND kind = 'location'
    )
    BEGIN
        SELECT RAISE(ABORT, 'business_locations subject must have kind location');
    END
    """,
    """
    CREATE TRIGGER business_locations_subject_kind_update
    BEFORE UPDATE OF id ON business_locations
    WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_subjects
        WHERE id = NEW.id AND kind = 'location'
    )
    BEGIN
        SELECT RAISE(ABORT, 'business_locations subject must have kind location');
    END
    """,
    """
    CREATE TABLE external_identifiers (
        id TEXT PRIMARY KEY,
        subject_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        namespace TEXT NOT NULL,
        value TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        first_observed_at TEXT NOT NULL,
        last_observed_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(subject_id) REFERENCES knowledge_subjects(id),
        FOREIGN KEY(source_id) REFERENCES sources(id),
        UNIQUE(source_id, namespace, value),
        CHECK(status IN ('active', 'superseded', 'retired', 'conflicted')),
        CHECK(length(namespace) > 0),
        CHECK(length(value) > 0)
    )
    """,
    """
    CREATE TRIGGER external_identifiers_identity_immutable
    BEFORE UPDATE OF subject_id, source_id, namespace, value ON external_identifiers
    WHEN NEW.subject_id IS NOT OLD.subject_id
      OR NEW.source_id IS NOT OLD.source_id
      OR NEW.namespace IS NOT OLD.namespace
      OR NEW.value IS NOT OLD.value
    BEGIN
        SELECT RAISE(ABORT, 'external identifier identity is immutable');
    END
    """,
    """
    CREATE INDEX ix_external_identifiers_subject
    ON external_identifiers(subject_id)
    """,
    """
    CREATE TABLE channels (
        id TEXT PRIMARY KEY,
        business_entity_id TEXT NOT NULL,
        location_id TEXT,
        channel_type TEXT NOT NULL,
        identifier TEXT NOT NULL,
        normalized_identifier TEXT NOT NULL,
        url TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        first_observed_at TEXT NOT NULL,
        last_verified_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(id) REFERENCES knowledge_subjects(id),
        FOREIGN KEY(business_entity_id) REFERENCES business_entities(id),
        FOREIGN KEY(location_id, business_entity_id)
            REFERENCES business_locations(id, business_entity_id),
        CHECK(channel_type IN (
            'website', 'phone', 'email', 'whatsapp', 'instagram', 'facebook',
            'linkedin', 'x', 'tiktok', 'youtube', 'booking', 'ordering',
            'commerce', 'marketplace', 'support', 'mobile_app', 'other'
        )),
        CHECK(status IN ('active', 'inactive', 'unverified', 'conflicted')),
        CHECK(length(identifier) > 0),
        CHECK(length(normalized_identifier) > 0)
    )
    """,
    """
    CREATE TRIGGER channels_subject_kind_insert
    BEFORE INSERT ON channels
    WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_subjects
        WHERE id = NEW.id AND kind = 'channel'
    )
    BEGIN
        SELECT RAISE(ABORT, 'channels subject must have kind channel');
    END
    """,
    """
    CREATE TRIGGER channels_subject_kind_update
    BEFORE UPDATE OF id ON channels
    WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_subjects
        WHERE id = NEW.id AND kind = 'channel'
    )
    BEGIN
        SELECT RAISE(ABORT, 'channels subject must have kind channel');
    END
    """,
    """
    CREATE INDEX ix_channels_entity
    ON channels(business_entity_id)
    """,
    """
    CREATE INDEX ix_channels_location
    ON channels(location_id)
    """,
    """
    CREATE TABLE acquisition_sessions (
        id TEXT PRIMARY KEY,
        target_subject_id TEXT,
        source_id TEXT NOT NULL,
        collector_name TEXT NOT NULL,
        collector_version TEXT NOT NULL,
        config_json TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        error TEXT,
        legacy_run_id TEXT UNIQUE,
        evidence_count INTEGER NOT NULL DEFAULT 0,
        observation_count INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(target_subject_id) REFERENCES knowledge_subjects(id),
        FOREIGN KEY(source_id) REFERENCES sources(id),
        FOREIGN KEY(legacy_run_id) REFERENCES runs(id),
        UNIQUE(id, source_id),
        CHECK(status IN ('planned', 'running', 'complete', 'partial', 'blocked', 'failed', 'cancelled')),
        CHECK(length(config_hash) = 64),
        CHECK(evidence_count >= 0),
        CHECK(observation_count >= 0),
        CHECK(
            (status IN ('planned', 'running') AND finished_at IS NULL AND error IS NULL)
            OR
            (status = 'complete' AND finished_at IS NOT NULL AND error IS NULL)
            OR
            (status IN ('partial', 'blocked', 'failed', 'cancelled') AND finished_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TRIGGER acquisition_sessions_identity_immutable
    BEFORE UPDATE OF id, target_subject_id, source_id, collector_name, collector_version,
                     config_json, config_hash, started_at, legacy_run_id
    ON acquisition_sessions
    WHEN NEW.id IS NOT OLD.id
      OR NEW.target_subject_id IS NOT OLD.target_subject_id
      OR NEW.source_id IS NOT OLD.source_id
      OR NEW.collector_name IS NOT OLD.collector_name
      OR NEW.collector_version IS NOT OLD.collector_version
      OR NEW.config_json IS NOT OLD.config_json
      OR NEW.config_hash IS NOT OLD.config_hash
      OR NEW.started_at IS NOT OLD.started_at
      OR NEW.legacy_run_id IS NOT OLD.legacy_run_id
    BEGIN
        SELECT RAISE(ABORT, 'acquisition session identity/configuration is immutable');
    END
    """,
    """
    CREATE TRIGGER acquisition_sessions_status_transition
    BEFORE UPDATE OF status ON acquisition_sessions
    WHEN NEW.status <> OLD.status
      AND NOT (
          (OLD.status = 'planned' AND NEW.status IN (
              'running', 'complete', 'partial', 'blocked', 'failed', 'cancelled'
          ))
          OR
          (OLD.status = 'running' AND NEW.status IN (
              'complete', 'partial', 'blocked', 'failed', 'cancelled'
          ))
      )
    BEGIN
        SELECT RAISE(ABORT, 'invalid acquisition session status transition');
    END
    """,
    """
    CREATE TRIGGER acquisition_sessions_terminal_immutable
    BEFORE UPDATE OF status, finished_at, error ON acquisition_sessions
    WHEN OLD.status IN ('complete', 'partial', 'blocked', 'failed', 'cancelled')
      AND (
          NEW.status IS NOT OLD.status
          OR NEW.finished_at IS NOT OLD.finished_at
          OR NEW.error IS NOT OLD.error
      )
    BEGIN
        SELECT RAISE(ABORT, 'terminal acquisition session lifecycle is immutable');
    END
    """,
    """
    CREATE TRIGGER acquisition_sessions_no_delete
    BEFORE DELETE ON acquisition_sessions
    BEGIN
        SELECT RAISE(ABORT, 'acquisition sessions are durable history');
    END
    """,
    """
    CREATE INDEX ix_acquisition_sessions_target
    ON acquisition_sessions(target_subject_id)
    """,
    """
    CREATE INDEX ix_acquisition_sessions_source_status
    ON acquisition_sessions(source_id, status)
    """,
    """
    CREATE TABLE evidence_items (
        id TEXT PRIMARY KEY,
        acquisition_session_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_locator TEXT,
        source_role TEXT NOT NULL,
        status TEXT NOT NULL,
        retrieved_at TEXT NOT NULL,
        published_at TEXT,
        language TEXT,
        media_type TEXT,
        content_sha256 TEXT,
        artifact_ref TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY(acquisition_session_id) REFERENCES acquisition_sessions(id),
        FOREIGN KEY(source_id) REFERENCES sources(id),
        FOREIGN KEY(acquisition_session_id, source_id)
            REFERENCES acquisition_sessions(id, source_id),
        CHECK(source_role IN (
            'official', 'platform', 'third_party', 'customer_generated',
            'public_authority', 'unknown'
        )),
        CHECK(status IN ('usable', 'incomplete', 'unavailable', 'blocked', 'malformed')),
        CHECK(content_sha256 IS NULL OR length(content_sha256) = 64)
    )
    """,
    """
    CREATE TRIGGER evidence_items_immutable
    BEFORE UPDATE ON evidence_items
    BEGIN
        SELECT RAISE(ABORT, 'evidence items are immutable');
    END
    """,
    """
    CREATE TRIGGER evidence_items_no_delete
    BEFORE DELETE ON evidence_items
    BEGIN
        SELECT RAISE(ABORT, 'evidence items are append-only');
    END
    """,
    """
    CREATE INDEX ix_evidence_session
    ON evidence_items(acquisition_session_id)
    """,
    """
    CREATE INDEX ix_evidence_source_locator
    ON evidence_items(source_id, source_locator)
    """,
    """
    CREATE INDEX ix_evidence_content_hash
    ON evidence_items(content_sha256)
    """,
    """
    CREATE TABLE predicate_definitions (
        name TEXT PRIMARY KEY,
        domain TEXT NOT NULL,
        subject_kind TEXT NOT NULL,
        value_type TEXT NOT NULL,
        cardinality TEXT NOT NULL,
        reconciliation_policy TEXT NOT NULL,
        freshness_days INTEGER,
        description TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        CHECK(domain IN (
            'identity', 'classification', 'locations', 'offerings',
            'customer_market', 'business_model', 'scale', 'communication',
            'digital_presence', 'digital_capabilities', 'customer_journey',
            'reputation', 'marketing', 'technology', 'people', 'operations',
            'change', 'competitive_context'
        )),
        CHECK(subject_kind IN ('business_entity', 'location', 'channel')),
        CHECK(value_type IN (
            'text', 'integer', 'real', 'boolean', 'json', 'date',
            'datetime', 'url'
        )),
        CHECK(cardinality IN ('single', 'multi')),
        CHECK(freshness_days IS NULL OR freshness_days > 0),
        CHECK(active IN (0, 1))
    )
    """,
    """
    CREATE TRIGGER predicate_definitions_semantics_immutable
    BEFORE UPDATE OF name, domain, subject_kind, value_type, cardinality, reconciliation_policy
    ON predicate_definitions
    WHEN NEW.name IS NOT OLD.name
      OR NEW.domain IS NOT OLD.domain
      OR NEW.subject_kind IS NOT OLD.subject_kind
      OR NEW.value_type IS NOT OLD.value_type
      OR NEW.cardinality IS NOT OLD.cardinality
      OR NEW.reconciliation_policy IS NOT OLD.reconciliation_policy
    BEGIN
        SELECT RAISE(ABORT, 'predicate semantics are immutable');
    END
    """,
    """
    CREATE INDEX ix_predicate_definitions_domain
    ON predicate_definitions(domain, active)
    """,
    """
    CREATE TABLE observations (
        id TEXT PRIMARY KEY,
        subject_id TEXT NOT NULL,
        predicate TEXT NOT NULL,
        evidence_id TEXT NOT NULL,
        value_json TEXT,
        normalized_value_json TEXT,
        value_hash TEXT,
        observation_kind TEXT NOT NULL,
        observed_at TEXT,
        extracted_at TEXT NOT NULL,
        extraction_method TEXT NOT NULL,
        extractor_name TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        confidence REAL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(subject_id) REFERENCES knowledge_subjects(id),
        FOREIGN KEY(predicate) REFERENCES predicate_definitions(name),
        FOREIGN KEY(evidence_id) REFERENCES evidence_items(id),
        CHECK(observation_kind IN (
            'source_assertion', 'structured_value', 'detected_capability',
            'derived_observation'
        )),
        CHECK(extraction_method IN (
            'direct_structured', 'deterministic_parser', 'heuristic',
            'model', 'human_verified', 'legacy_import'
        )),
        CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
        CHECK(value_hash IS NULL OR length(value_hash) = 64)
    )
    """,
    """
    CREATE TRIGGER observations_subject_kind_insert
    BEFORE INSERT ON observations
    WHEN NOT EXISTS (
        SELECT 1
        FROM knowledge_subjects ks
        JOIN predicate_definitions pd ON pd.name = NEW.predicate
        WHERE ks.id = NEW.subject_id AND ks.kind = pd.subject_kind
    )
    BEGIN
        SELECT RAISE(ABORT, 'observation subject kind does not match predicate');
    END
    """,
    """
    CREATE TRIGGER observations_immutable
    BEFORE UPDATE ON observations
    BEGIN
        SELECT RAISE(ABORT, 'observations are immutable');
    END
    """,
    """
    CREATE TRIGGER observations_no_delete
    BEFORE DELETE ON observations
    BEGIN
        SELECT RAISE(ABORT, 'observations are append-only');
    END
    """,
    """
    CREATE INDEX ix_observations_subject_predicate
    ON observations(subject_id, predicate)
    """,
    """
    CREATE INDEX ix_observations_evidence
    ON observations(evidence_id)
    """,
    """
    CREATE TABLE facts (
        id TEXT PRIMARY KEY,
        subject_id TEXT NOT NULL,
        predicate TEXT NOT NULL,
        fact_slot TEXT NOT NULL,
        value_json TEXT,
        normalized_value_json TEXT,
        value_hash TEXT,
        status TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        last_verified_at TEXT,
        reconciled_at TEXT NOT NULL,
        reconciliation_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(subject_id) REFERENCES knowledge_subjects(id),
        FOREIGN KEY(predicate) REFERENCES predicate_definitions(name),
        CHECK(status IN (
            'confirmed', 'single_source', 'conflicted', 'stale',
            'unknown', 'not_observed', 'not_applicable'
        )),
        CHECK(length(fact_slot) > 0),
        CHECK(value_hash IS NULL OR length(value_hash) = 64),
        CHECK(
            status NOT IN ('unknown', 'not_observed', 'not_applicable')
            OR (value_json IS NULL AND normalized_value_json IS NULL AND value_hash IS NULL)
        )
    )
    """,
    """
    CREATE TRIGGER facts_subject_kind_insert
    BEFORE INSERT ON facts
    WHEN NOT EXISTS (
        SELECT 1
        FROM knowledge_subjects ks
        JOIN predicate_definitions pd ON pd.name = NEW.predicate
        WHERE ks.id = NEW.subject_id AND ks.kind = pd.subject_kind
    )
    BEGIN
        SELECT RAISE(ABORT, 'fact subject kind does not match predicate');
    END
    """,
    """
    CREATE TRIGGER facts_version_immutable
    BEFORE UPDATE ON facts
    WHEN NEW.id IS NOT OLD.id
      OR NEW.subject_id IS NOT OLD.subject_id
      OR NEW.predicate IS NOT OLD.predicate
      OR NEW.fact_slot IS NOT OLD.fact_slot
      OR NEW.value_json IS NOT OLD.value_json
      OR NEW.normalized_value_json IS NOT OLD.normalized_value_json
      OR NEW.value_hash IS NOT OLD.value_hash
      OR NEW.status IS NOT OLD.status
      OR NEW.valid_from IS NOT OLD.valid_from
      OR NEW.last_verified_at IS NOT OLD.last_verified_at
      OR NEW.reconciled_at IS NOT OLD.reconciled_at
      OR NEW.reconciliation_version IS NOT OLD.reconciliation_version
      OR NEW.created_at IS NOT OLD.created_at
      OR OLD.valid_to IS NOT NULL
      OR NEW.valid_to IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'fact versions are immutable except for first close');
    END
    """,
    """
    CREATE TRIGGER facts_no_delete
    BEFORE DELETE ON facts
    BEGIN
        SELECT RAISE(ABORT, 'fact history is append-only');
    END
    """,
    """
    CREATE UNIQUE INDEX ux_facts_current_slot
    ON facts(subject_id, predicate, fact_slot)
    WHERE valid_to IS NULL
    """,
    """
    CREATE INDEX ix_facts_current_subject
    ON facts(subject_id, predicate)
    WHERE valid_to IS NULL
    """,
    """
    CREATE TABLE fact_observation_support (
        fact_id TEXT NOT NULL,
        observation_id TEXT NOT NULL,
        support_role TEXT NOT NULL,
        PRIMARY KEY(fact_id, observation_id),
        FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE,
        FOREIGN KEY(observation_id) REFERENCES observations(id),
        CHECK(support_role IN ('supports', 'contradicts', 'supersedes'))
    )
    """,
    """
    CREATE TRIGGER fact_observation_support_immutable
    BEFORE UPDATE ON fact_observation_support
    BEGIN
        SELECT RAISE(ABORT, 'fact observation support is immutable');
    END
    """,
    """
    CREATE TRIGGER fact_observation_support_no_delete
    BEFORE DELETE ON fact_observation_support
    BEGIN
        SELECT RAISE(ABORT, 'fact observation support is append-only');
    END
    """,
    """
    CREATE TABLE fact_acquisition_support (
        fact_id TEXT NOT NULL,
        acquisition_session_id TEXT NOT NULL,
        support_role TEXT NOT NULL,
        PRIMARY KEY(fact_id, acquisition_session_id),
        FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE,
        FOREIGN KEY(acquisition_session_id) REFERENCES acquisition_sessions(id),
        CHECK(support_role IN ('searched', 'supports_absence', 'context'))
    )
    """,
    """
    CREATE TRIGGER fact_acquisition_support_immutable
    BEFORE UPDATE ON fact_acquisition_support
    BEGIN
        SELECT RAISE(ABORT, 'fact acquisition support is immutable');
    END
    """,
    """
    CREATE TRIGGER fact_acquisition_support_no_delete
    BEFORE DELETE ON fact_acquisition_support
    BEGIN
        SELECT RAISE(ABORT, 'fact acquisition support is append-only');
    END
    """,
    """
    CREATE TABLE business_relationships (
        id TEXT PRIMARY KEY,
        from_entity_id TEXT NOT NULL,
        to_entity_id TEXT NOT NULL,
        relationship_type TEXT NOT NULL,
        status TEXT NOT NULL,
        first_observed_at TEXT NOT NULL,
        last_verified_at TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(from_entity_id) REFERENCES business_entities(id),
        FOREIGN KEY(to_entity_id) REFERENCES business_entities(id),
        CHECK(from_entity_id <> to_entity_id),
        CHECK(relationship_type IN (
            'parent_brand', 'subsidiary', 'franchise_of', 'operated_by',
            'owned_by', 'brand_of'
        )),
        CHECK(status IN ('asserted', 'confirmed', 'conflicted', 'retired'))
    )
    """,
    """
    CREATE INDEX ix_business_relationships_from
    ON business_relationships(from_entity_id, relationship_type)
    """,
    """
    CREATE INDEX ix_business_relationships_to
    ON business_relationships(to_entity_id, relationship_type)
    """,
    """
    CREATE TABLE business_relationship_observation_support (
        relationship_id TEXT NOT NULL,
        observation_id TEXT NOT NULL,
        support_role TEXT NOT NULL,
        PRIMARY KEY(relationship_id, observation_id),
        FOREIGN KEY(relationship_id) REFERENCES business_relationships(id) ON DELETE CASCADE,
        FOREIGN KEY(observation_id) REFERENCES observations(id),
        CHECK(support_role IN ('supports', 'contradicts'))
    )
    """,
    """
    CREATE TRIGGER business_relationship_observation_support_immutable
    BEFORE UPDATE ON business_relationship_observation_support
    BEGIN
        SELECT RAISE(ABORT, 'business relationship observation support is immutable');
    END
    """,
    """
    CREATE TRIGGER business_relationship_observation_support_no_delete
    BEFORE DELETE ON business_relationship_observation_support
    BEGIN
        SELECT RAISE(ABORT, 'business relationship observation support is append-only');
    END
    """,
    """
    CREATE TABLE dossier_assessments (
        id TEXT PRIMARY KEY,
        business_entity_id TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        facts_as_of TEXT NOT NULL,
        analysis_ready INTEGER NOT NULL,
        computed_at TEXT NOT NULL,
        summary_json TEXT NOT NULL,
        FOREIGN KEY(business_entity_id) REFERENCES business_entities(id),
        CHECK(analysis_ready IN (0, 1))
    )
    """,
    """
    CREATE TRIGGER dossier_assessments_immutable
    BEFORE UPDATE ON dossier_assessments
    BEGIN
        SELECT RAISE(ABORT, 'dossier assessments are immutable snapshots');
    END
    """,
    """
    CREATE TRIGGER dossier_assessments_no_delete
    BEFORE DELETE ON dossier_assessments
    BEGIN
        SELECT RAISE(ABORT, 'dossier assessments are immutable snapshots');
    END
    """,
    """
    CREATE INDEX ix_dossier_assessments_entity_policy
    ON dossier_assessments(business_entity_id, policy_version, computed_at)
    """,
    """
    CREATE TABLE dossier_domain_assessments (
        assessment_id TEXT NOT NULL,
        domain TEXT NOT NULL,
        state TEXT NOT NULL,
        reason_json TEXT NOT NULL,
        fact_count INTEGER NOT NULL,
        fresh_fact_count INTEGER NOT NULL,
        PRIMARY KEY(assessment_id, domain),
        FOREIGN KEY(assessment_id) REFERENCES dossier_assessments(id) ON DELETE CASCADE,
        CHECK(domain IN (
            'identity', 'classification', 'locations', 'offerings',
            'customer_market', 'business_model', 'scale', 'communication',
            'digital_presence', 'digital_capabilities', 'customer_journey',
            'reputation', 'marketing', 'technology', 'people', 'operations',
            'change', 'competitive_context', 'provenance', 'unknowns'
        )),
        CHECK(state IN (
            'not_started', 'insufficient', 'partial', 'sufficient', 'strong',
            'stale', 'conflicted', 'not_applicable'
        )),
        CHECK(fact_count >= 0),
        CHECK(fresh_fact_count >= 0),
        CHECK(fresh_fact_count <= fact_count)
    )
    """,
    """
    CREATE TRIGGER dossier_domain_assessments_immutable
    BEFORE UPDATE ON dossier_domain_assessments
    BEGIN
        SELECT RAISE(ABORT, 'dossier domain assessments are immutable snapshots');
    END
    """,
    """
    CREATE TRIGGER dossier_domain_assessments_no_delete
    BEFORE DELETE ON dossier_domain_assessments
    BEGIN
        SELECT RAISE(ABORT, 'dossier domain assessments are immutable snapshots');
    END
    """,
)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="business_understanding_foundation_v1",
        statements=BUSINESS_UNDERSTANDING_V1,
    ),
)


_REQUIRED_CORE_COLUMNS: dict[str, frozenset[str]] = {
    "runs": frozenset({
        "id", "area_name", "bbox_json", "cell_km", "depth", "queries_json",
        "scraper_image", "config_json", "raw_path", "status", "started_at",
        "finished_at", "exit_code", "error", "raw_records", "accepted_records",
        "out_of_bounds_records", "unlocated_records", "unidentified_records",
        "unique_seen", "new_businesses",
    }),
    "businesses": frozenset({
        "id", "canonical_key", "place_id", "cid", "data_id", "title",
        "category", "address", "latitude", "longitude", "phone", "website",
        "review_rating", "review_count", "status", "first_seen_at",
        "last_seen_at", "first_run_id", "last_run_id", "raw_json",
    }),
    "run_businesses": frozenset({"run_id", "business_id", "first_observed_at"}),
}


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})"))


def validate_core_schema(conn: sqlite3.Connection) -> None:
    """Fail closed unless the connection exposes the current Sara core schema.

    Recovery tables are deliberately not required: they are lazily created by
    recovery-run and are orthogonal to Business Understanding migrations.
    """
    for table, required in _REQUIRED_CORE_COLUMNS.items():
        actual = _table_columns(conn, table)
        if not actual:
            raise MigrationError(f"required Sara core table {table!r} does not exist")
        missing = sorted(required - actual)
        if missing:
            raise MigrationError(
                f"Sara core table {table!r} is missing required columns: {missing!r}"
            )
    _validate_core_constraints(conn)


def _validate_migration_registry(migrations: Sequence[Migration]) -> None:
    versions = [migration.version for migration in migrations]
    if versions != sorted(versions) or len(versions) != len(set(versions)):
        raise MigrationError("migration versions must be unique and strictly ordered")
    names = [migration.name for migration in migrations]
    if len(names) != len(set(names)):
        raise MigrationError("migration names must be unique")
    if any(version <= 0 for version in versions):
        raise MigrationError("migration versions must be positive")
    if versions and versions != list(range(1, versions[-1] + 1)):
        raise MigrationError("migration versions must be contiguous starting at 1")


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = list(conn.execute(f"PRAGMA table_info({table})"))
    return [
        str(row[1])
        for row in sorted((row for row in rows if int(row[5]) > 0), key=lambda row: int(row[5]))
    ]


def _foreign_keys(conn: sqlite3.Connection, table: str) -> set[tuple[str, str, str, str]]:
    return {
        (str(row[2]), str(row[3]), str(row[4]), str(row[6]))
        for row in conn.execute(f"PRAGMA foreign_key_list({table})")
    }


def _has_unique_index(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    *,
    partial: bool | None = None,
) -> bool:
    for row in conn.execute(f"PRAGMA index_list({table})"):
        if int(row[2]) != 1:
            continue
        if partial is not None and bool(row[4]) is not partial:
            continue
        index_columns = [
            str(info[2]) for info in conn.execute(f"PRAGMA index_info({row[1]})")
        ]
        if index_columns == columns:
            return True
    return False


def _has_expected_partial_unique_index(
    conn: sqlite3.Connection,
    table: str,
    column: str,
) -> bool:
    expected_where = f"where {column.lower()} is not null and {column.lower()} <> ''"
    for row in conn.execute(f"PRAGMA index_list({table})"):
        if int(row[2]) != 1 or int(row[4]) != 1:
            continue
        index_columns = [
            str(info[2]) for info in conn.execute(f"PRAGMA index_info({row[1]})")
        ]
        if index_columns != [column]:
            continue
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (row[1],),
        ).fetchone()
        if sql_row is None or sql_row[0] is None:
            continue
        normalized_sql = " ".join(str(sql_row[0]).lower().split())
        if expected_where in normalized_sql:
            return True
    return False


def _validate_core_constraints(conn: sqlite3.Connection) -> None:
    expected_pks = {
        "runs": ["id"],
        "businesses": ["id"],
        "run_businesses": ["run_id", "business_id"],
    }
    for table, expected in expected_pks.items():
        actual = _primary_key_columns(conn, table)
        if actual != expected:
            raise MigrationError(
                f"Sara core table {table!r} has incompatible primary key: {actual!r}"
            )

    if not _has_unique_index(conn, "businesses", ["canonical_key"], partial=False):
        raise MigrationError("Sara core table 'businesses' is missing canonical_key uniqueness")
    for column in ("place_id", "cid", "data_id"):
        if not _has_expected_partial_unique_index(conn, "businesses", column):
            raise MigrationError(
                "Sara core table 'businesses' is missing the required partial uniqueness "
                f"predicate for {column!r}"
            )

    business_fks = _foreign_keys(conn, "businesses")
    for expected in {
        ("runs", "first_run_id", "id", "NO ACTION"),
        ("runs", "last_run_id", "id", "NO ACTION"),
    }:
        if expected not in business_fks:
            raise MigrationError(
                f"Sara core table 'businesses' is missing required foreign key {expected!r}"
            )

    membership_fks = _foreign_keys(conn, "run_businesses")
    for expected in {
        ("runs", "run_id", "id", "CASCADE"),
        ("businesses", "business_id", "id", "CASCADE"),
    }:
        if expected not in membership_fks:
            raise MigrationError(
                f"Sara core table 'run_businesses' is missing required foreign key {expected!r}"
            )


def _validate_schema_migrations_table(conn: sqlite3.Connection) -> None:
    rows = list(conn.execute("PRAGMA table_info(schema_migrations)"))
    expected = [
        ("version", "INTEGER", 1),
        ("name", "TEXT", 0),
        ("checksum", "TEXT", 0),
        ("applied_at", "TEXT", 0),
    ]
    actual = [(str(row[1]), str(row[2]), int(row[5])) for row in rows]
    if actual != expected:
        raise MigrationError(
            f"schema_migrations has an incompatible layout: {actual!r}"
        )
    for row in rows[1:]:
        if int(row[3]) != 1:
            raise MigrationError(
                f"schema_migrations column {row[1]!r} must be NOT NULL"
            )
    if not _has_unique_index(conn, "schema_migrations", ["name"], partial=False):
        raise MigrationError("schema_migrations.name must have a full UNIQUE constraint")


def _load_applied(conn: sqlite3.Connection) -> dict[int, tuple[str, str, str]]:
    return {
        int(row[0]): (str(row[1]), str(row[2]), str(row[3]))
        for row in conn.execute(
            "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
        )
    }


def _verify_applied_migrations(
    applied: dict[int, tuple[str, str, str]], migrations: Sequence[Migration]
) -> None:
    known = {migration.version: migration for migration in migrations}
    applied_versions = sorted(applied)
    if applied_versions and applied_versions != list(range(1, applied_versions[-1] + 1)):
        raise MigrationError(
            f"database schema migration history is not a contiguous prefix: {applied_versions!r}"
        )
    for version, row in applied.items():
        migration = known.get(version)
        if migration is None:
            raise MigrationError(
                f"database has unknown schema migration version {version}; "
                "this Sara build cannot verify it"
            )
        database_name, database_checksum, _applied_at = row
        if database_name != migration.name:
            raise MigrationError(
                f"schema migration {version} name mismatch: "
                f"database={database_name!r}, code={migration.name!r}"
            )
        if database_checksum != migration.checksum:
            raise MigrationError(
                f"schema migration {version} checksum mismatch; "
                "an applied migration was modified"
            )


def _apply_statements(conn: sqlite3.Connection, statements: Iterable[str]) -> None:
    for statement in statements:
        conn.execute(statement.strip())


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> tuple[int, ...]:
    """Apply pending Business Understanding schema migrations transactionally.

    This function is intentionally explicit in Phase 0/1. It is *not* called
    automatically by ``storage.connect`` yet, so introducing the migration
    framework cannot silently mutate an existing production database merely
    because an existing Sara command opened it. Automatic integration is a
    later, separately reviewed step.

    Returns the migration versions applied by this call. An already-current
    database returns an empty tuple.
    """
    _validate_migration_registry(migrations)

    if conn.in_transaction:
        raise MigrationError("apply_migrations requires a connection with no active transaction")

    conn.execute("PRAGMA foreign_keys = ON")
    foreign_keys_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if foreign_keys_enabled != 1:
        raise MigrationError("SQLite foreign-key enforcement must be enabled for migrations")

    try:
        conn.execute("BEGIN IMMEDIATE")
        validate_core_schema(conn)
        conn.execute(_SCHEMA_MIGRATIONS_SQL.strip())
        _validate_schema_migrations_table(conn)
        applied = _load_applied(conn)
        _verify_applied_migrations(applied, migrations)

        applied_now: list[int] = []
        for migration in migrations:
            if migration.version in applied:
                continue
            _apply_statements(conn, migration.statements)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, _utc_now()),
            )
            applied_now.append(migration.version)

        foreign_key_violations = list(conn.execute("PRAGMA foreign_key_check"))
        if foreign_key_violations:
            raise MigrationError(
                "foreign-key violations detected after schema migration: "
                f"{foreign_key_violations!r}"
            )
        conn.commit()
        return tuple(applied_now)
    except BaseException:
        conn.rollback()
        raise


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the latest applied Business Understanding migration version.

    A database on which the migration framework has never been applied
    reports version 0. This function never creates schema.
    """
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if table is None:
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0])
