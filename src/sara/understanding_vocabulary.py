from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass

from .migrations import MIGRATIONS, current_schema_version


class VocabularySeedError(RuntimeError):
    """The controlled Business Understanding vocabulary cannot be seeded safely."""


@dataclass(frozen=True)
class PredicateSeed:
    name: str
    domain: str
    subject_kind: str
    value_type: str
    cardinality: str
    reconciliation_policy: str
    freshness_days: int | None
    description: str
    active: int = 1


@dataclass(frozen=True)
class DossierDomainSeed:
    name: str
    mandatory_for_initial_analysis: bool
    description: str


VOCABULARY_VERSION = "business-understanding-v1"
DOSSIER_POLICY_VERSION = "business-understanding-v1"


PREDICATE_SEED_V1: tuple[PredicateSeed, ...] = (
    PredicateSeed(
        "business.name.trading",
        "identity",
        "business_entity",
        "text",
        "single",
        "prefer_authoritative_recent",
        90,
        "Current public trading name used by the business.",
    ),
    PredicateSeed(
        "business.category.primary",
        "classification",
        "business_entity",
        "text",
        "single",
        "prefer_authoritative_recent",
        90,
        "Primary public business category or activity classification.",
    ),
    PredicateSeed(
        "business.website.official",
        "digital_presence",
        "business_entity",
        "url",
        "single",
        "prefer_authoritative_recent",
        90,
        "Official website URL whose relationship to the business is supported by evidence.",
    ),
    PredicateSeed(
        "business.model.transaction_type",
        "business_model",
        "business_entity",
        "text",
        "multi",
        "supported_set",
        90,
        "Supported customer transaction modes such as walk-in, booking, ordering, or quote-based sale.",
    ),
    PredicateSeed(
        "business.customer_segment.stated",
        "customer_market",
        "business_entity",
        "text",
        "multi",
        "supported_set",
        90,
        "Customer segment explicitly stated in retained source evidence; inferred segments require a separate future predicate.",
    ),
    PredicateSeed(
        "business.offering.service",
        "offerings",
        "business_entity",
        "text",
        "multi",
        "supported_set",
        90,
        "Publicly evidenced service or service family offered by the business.",
    ),
    PredicateSeed(
        "location.address",
        "locations",
        "location",
        "text",
        "single",
        "prefer_authoritative_recent",
        90,
        "Current public address for a business location.",
    ),
    PredicateSeed(
        "location.latitude",
        "locations",
        "location",
        "real",
        "single",
        "prefer_authoritative_recent",
        90,
        "Latitude of a business location from retained source evidence.",
    ),
    PredicateSeed(
        "location.longitude",
        "locations",
        "location",
        "real",
        "single",
        "prefer_authoritative_recent",
        90,
        "Longitude of a business location from retained source evidence.",
    ),
    PredicateSeed(
        "location.phone",
        "communication",
        "location",
        "text",
        "multi",
        "supported_set",
        60,
        "Public phone number explicitly associated with a business location.",
    ),
    PredicateSeed(
        "location.opening_hours",
        "operations",
        "location",
        "json",
        "single",
        "latest_observed",
        30,
        "Structured public operating hours for a business location.",
    ),
    PredicateSeed(
        "capability.online_booking",
        "digital_capabilities",
        "business_entity",
        "boolean",
        "single",
        "latest_observed",
        30,
        "Whether retained evidence establishes an online booking or reservation capability.",
    ),
    PredicateSeed(
        "capability.online_ordering",
        "digital_capabilities",
        "business_entity",
        "boolean",
        "single",
        "latest_observed",
        30,
        "Whether retained evidence establishes an online ordering capability.",
    ),
    PredicateSeed(
        "capability.whatsapp",
        "digital_capabilities",
        "business_entity",
        "boolean",
        "single",
        "latest_observed",
        30,
        "Whether retained evidence establishes an official customer-facing WhatsApp action or channel.",
    ),
    PredicateSeed(
        "reputation.rating",
        "reputation",
        "location",
        "real",
        "single",
        "latest_platform_metric",
        30,
        "Current rating metric for a location from a retained review/platform source.",
    ),
    PredicateSeed(
        "reputation.review_count",
        "reputation",
        "location",
        "integer",
        "single",
        "latest_platform_metric",
        30,
        "Current review-count metric for a location from a retained review/platform source.",
    ),
)


DOSSIER_DOMAIN_SEED_V1: tuple[DossierDomainSeed, ...] = (
    DossierDomainSeed("identity", True, "Who exactly is this business?"),
    DossierDomainSeed("classification", True, "What kind of business is it?"),
    DossierDomainSeed("locations", True, "Where and at what geographic scale does it operate?"),
    DossierDomainSeed("offerings", True, "What products or services does it actually provide?"),
    DossierDomainSeed("customer_market", False, "Who does the business appear to serve and why?"),
    DossierDomainSeed("business_model", True, "How does a customer transact with the business?"),
    DossierDomainSeed("scale", False, "What observable scale and operating-footprint signals exist?"),
    DossierDomainSeed("communication", True, "How can a prospective or existing customer communicate with it?"),
    DossierDomainSeed("digital_presence", True, "Where does the business exist digitally?"),
    DossierDomainSeed("digital_capabilities", True, "What can a customer actually accomplish digitally?"),
    DossierDomainSeed("customer_journey", True, "What major observable customer-interaction stages can be reconstructed?"),
    DossierDomainSeed("reputation", True, "What current evidence exists about reputation and customer voice?"),
    DossierDomainSeed("marketing", False, "What observable customer-acquisition and marketing mechanisms are used?"),
    DossierDomainSeed("technology", False, "What externally observable technology is evidenced?"),
    DossierDomainSeed("people", False, "What public professional or organizational information is evidenced?"),
    DossierDomainSeed("operations", False, "What publicly observable operating signals are evidenced?"),
    DossierDomainSeed("change", False, "What material public changes or momentum signals are evidenced?"),
    DossierDomainSeed("competitive_context", True, "What relevant peer context is established from observable facts?"),
    DossierDomainSeed("provenance", True, "Can material facts be traced back to retained evidence?"),
    DossierDomainSeed("unknowns", True, "Are important unresolved facts represented explicitly rather than invented?"),
)


def vocabulary_checksum() -> str:
    payload = {
        "vocabulary_version": VOCABULARY_VERSION,
        "dossier_policy_version": DOSSIER_POLICY_VERSION,
        "predicates": [asdict(seed) for seed in PREDICATE_SEED_V1],
        "dossier_domains": [asdict(seed) for seed in DOSSIER_DOMAIN_SEED_V1],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def mandatory_dossier_domains() -> tuple[str, ...]:
    return tuple(
        seed.name for seed in DOSSIER_DOMAIN_SEED_V1 if seed.mandatory_for_initial_analysis
    )


def _predicate_values(seed: PredicateSeed) -> tuple[object, ...]:
    return (
        seed.domain,
        seed.subject_kind,
        seed.value_type,
        seed.cardinality,
        seed.reconciliation_policy,
        seed.freshness_days,
        seed.description,
        seed.active,
    )


def _require_exact_phase_one_history(conn: sqlite3.Connection) -> None:
    if current_schema_version(conn) != 1:
        raise VocabularySeedError(
            "vocabulary seed v1 requires Business Understanding schema version 1 exactly"
        )
    expected = MIGRATIONS[0]
    row = conn.execute(
        "SELECT name, checksum FROM schema_migrations WHERE version = 1"
    ).fetchone()
    if row is None or tuple(row) != (expected.name, expected.checksum):
        raise VocabularySeedError(
            "Business Understanding migration v1 history does not match this Sara build"
        )


def seed_business_understanding_vocabulary(
    conn: sqlite3.Connection,
) -> tuple[str, ...]:
    """Install the v1 controlled predicate vocabulary into a migrated database.

    The operation is explicit, transactional, idempotent, and fail-closed. If a
    predicate name already exists, every seeded field must match this version
    exactly; Phase 2 never silently rewrites an existing semantic definition.

    Dossier-domain policy remains code-versioned in ``DOSSIER_DOMAIN_SEED_V1``.
    Phase 2 does not create assessments, facts, entities, or acquisition data.
    """
    if conn.in_transaction:
        raise VocabularySeedError(
            "vocabulary seeding requires a connection with no active transaction"
        )
    _require_exact_phase_one_history(conn)

    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise VocabularySeedError(
            "SQLite foreign-key enforcement must be enabled for vocabulary seeding"
        )

    inserted: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for seed in PREDICATE_SEED_V1:
            row = conn.execute(
                "SELECT domain, subject_kind, value_type, cardinality, "
                "reconciliation_policy, freshness_days, description, active "
                "FROM predicate_definitions WHERE name = ?",
                (seed.name,),
            ).fetchone()
            expected = _predicate_values(seed)
            if row is None:
                conn.execute(
                    "INSERT INTO predicate_definitions("
                    "name, domain, subject_kind, value_type, cardinality, "
                    "reconciliation_policy, freshness_days, description, active"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (seed.name, *expected),
                )
                inserted.append(seed.name)
                continue
            actual = tuple(row)
            if actual != expected:
                raise VocabularySeedError(
                    f"predicate seed drift for {seed.name!r}: "
                    f"database={actual!r}, expected={expected!r}"
                )
        conn.commit()
        return tuple(inserted)
    except BaseException:
        conn.rollback()
        raise
