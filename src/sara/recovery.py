from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .config import BoundingBox
from .grid import GridEstimate, estimate_grid, grid_dimensions

SCHEMA_VERSION = 1
PLAN_KIND = "sara.recovery_plan"
COMPLETION_CLAIM = "recorded_complete_not_reverified_by_recovery_plan"
BINNING_METHOD = "equal_bbox_partition_v1"
BIN_ORDERING = "row_south_to_north_then_column_west_to_east"
EDGE_RULE = "north_east_max_to_final_bin"

TIER_A = "A"
TIER_B = "B"
TIER_UNSELECTED = "unselected"

# The only configuration fields ever copied into a plan artifact. Unknown
# fields are omitted so legacy, manual, or future configurations can never
# leak credentials, proxy URLs, tokens, or local paths into the export; the
# raw configuration remains auditable through config_sha256.
CONFIG_PROJECTION_FIELDS = (
    "area_name",
    "bbox",
    "queries",
    "cell_km",
    "depth",
    "concurrency",
    "browser_pool_size",
    "pages_per_browser",
    "lang",
    "zoom",
    "resume",
    "image",
    "proxy_sha256",
    "strict_bounds",
)


def project_source_config(config: dict[str, Any]) -> dict[str, Any]:
    """Allowlisted projection of the parsed source configuration."""
    return {key: config[key] for key in CONFIG_PROJECTION_FIELDS if key in config}


@dataclass(frozen=True)
class RecoveryPolicy:
    """Caller-supplied recovery policy. Thresholds never have defaults."""

    policy_id: str
    tier_a_min: int
    tier_b_min: int
    recovery_cell_km: float

    def validate(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ValueError("policy_id must be a nonempty string")
        if len(self.policy_id) > 128:
            raise ValueError("policy_id must be at most 128 characters")
        if isinstance(self.tier_b_min, bool) or not isinstance(self.tier_b_min, int) or self.tier_b_min < 1:
            raise ValueError("tier_b_min must be an integer >= 1")
        if isinstance(self.tier_a_min, bool) or not isinstance(self.tier_a_min, int) or self.tier_a_min <= self.tier_b_min:
            raise ValueError("tier_a_min must be an integer greater than tier_b_min")
        if (
            isinstance(self.recovery_cell_km, bool)
            or not isinstance(self.recovery_cell_km, (int, float))
            or not math.isfinite(self.recovery_cell_km)
            or self.recovery_cell_km <= 0
        ):
            raise ValueError("recovery_cell_km must be a finite value greater than zero")


@dataclass(frozen=True)
class DensityBin:
    row: int
    column: int
    bbox: BoundingBox
    business_count: int
    tier: str
    selected: bool
    recovery_estimate: GridEstimate | None


@dataclass(frozen=True)
class RecoveryPlan:
    policy: RecoveryPolicy
    source_run: dict[str, Any]
    source_grid_estimate: GridEstimate
    rows: int
    columns: int
    associated_businesses: int
    bins: tuple[DensityBin, ...]
    full_uniform_estimate: GridEstimate

    @property
    def summary(self) -> dict[str, Any]:
        tier_a = sum(1 for b in self.bins if b.tier == TIER_A)
        tier_b = sum(1 for b in self.bins if b.tier == TIER_B)
        unselected = sum(1 for b in self.bins if b.tier == TIER_UNSELECTED)
        selected = tier_a + tier_b
        targeted = sum(b.recovery_estimate.searches for b in self.bins if b.selected)
        uniform = self.full_uniform_estimate.searches
        return {
            "tier_a_bins": tier_a,
            "tier_b_bins": tier_b,
            "unselected_bins": unselected,
            "selected_bins": selected,
            "total_bins": len(self.bins),
            "estimated_recovery_searches": targeted,
            "full_uniform_recovery_searches": uniform,
            # Signed: positive means the independent-bin plan costs MORE than
            # one uniform recovery pass over the whole source bbox.
            "search_delta_vs_uniform": targeted - uniform,
            "recovery_fraction_of_full": (targeted / uniform) if uniform else 0.0,
        }


def build_bin_edges(minimum: float, maximum: float, count: int) -> list[float]:
    """Explicit partition edges with first and last pinned to the exact bounds.

    Internal edges are computed as minimum + index * height; the final edge is
    replaced with the exact maximum so accumulated floating error can never
    push an emitted bin edge past (or short of) the source bounding box.
    """
    if count < 1:
        raise ValueError("edge count must be at least 1")
    height = (maximum - minimum) / count
    edges = [minimum + index * height for index in range(count)]
    edges.append(maximum)
    return edges


def assign_density_bin(
    latitude: float,
    longitude: float,
    bbox: BoundingBox,
    rows: int,
    columns: int,
    *,
    lat_edges: Sequence[float] | None = None,
    lon_edges: Sequence[float] | None = None,
) -> tuple[int, int]:
    """Map a coordinate to its density bin using explicit edge arrays.

    Rows run south to north, columns west to east. ``bisect_right`` places an
    exact internal boundary into the higher bin without the floating-point
    instability of normalizing and flooring a ratio. The exact north/east
    maximum belongs to the final row/column. Nonfinite and out-of-bounds
    coordinates are errors; they are never silently clamped.
    """
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ValueError(f"nonfinite coordinate: ({latitude!r}, {longitude!r})")
    if not bbox.contains(latitude, longitude):
        raise ValueError(f"coordinate ({latitude!r}, {longitude!r}) lies outside the source bounding box")
    if lat_edges is None:
        lat_edges = build_bin_edges(bbox.min_lat, bbox.max_lat, rows)
    if lon_edges is None:
        lon_edges = build_bin_edges(bbox.min_lon, bbox.max_lon, columns)
    row = bisect_right(lat_edges, latitude) - 1
    column = bisect_right(lon_edges, longitude) - 1
    if row < 0 or column < 0:
        raise ValueError(f"coordinate ({latitude!r}, {longitude!r}) lies outside the source bounding box")
    if row >= rows:
        row = rows - 1
    if column >= columns:
        column = columns - 1
    return row, column


def build_density_bins(
    bbox: BoundingBox,
    rows: int,
    columns: int,
    *,
    lat_edges: Sequence[float] | None = None,
    lon_edges: Sequence[float] | None = None,
) -> list[BoundingBox]:
    """Evenly partition the bbox into rows x columns bin bboxes.

    The partition uses the same explicit edge arrays as bin assignment, so an
    emitted bin boundary and the assignment of a coordinate sitting exactly on
    that boundary can never disagree. Order is deterministic: row ascending
    (south to north), then column ascending (west to east).
    """
    if rows < 1 or columns < 1:
        raise ValueError("density grid must have at least one row and one column")
    if lat_edges is None:
        lat_edges = build_bin_edges(bbox.min_lat, bbox.max_lat, rows)
    if lon_edges is None:
        lon_edges = build_bin_edges(bbox.min_lon, bbox.max_lon, columns)
    bins: list[BoundingBox] = []
    for row in range(rows):
        for column in range(columns):
            bins.append(
                BoundingBox(
                    min_lat=lat_edges[row],
                    min_lon=lon_edges[column],
                    max_lat=lat_edges[row + 1],
                    max_lon=lon_edges[column + 1],
                )
            )
    return bins


def classify_density(business_count: int, policy: RecoveryPolicy) -> str:
    if business_count >= policy.tier_a_min:
        return TIER_A
    if business_count >= policy.tier_b_min:
        return TIER_B
    return TIER_UNSELECTED


def build_recovery_plan(
    *,
    policy: RecoveryPolicy,
    source_run: dict[str, Any],
    source_bbox: BoundingBox,
    source_cell_km: float,
    query_count: int,
    coordinates: Iterable[tuple[float, float]],
) -> RecoveryPlan:
    """Build a planning-only recovery plan from run membership coordinates.

    Every selected bin is estimated independently with the real Sara grid
    estimator. Bins are never merged, and no estimate is derived from a
    selected-count shortcut.
    """
    policy.validate()
    if isinstance(source_cell_km, bool) or not isinstance(source_cell_km, (int, float)) or not math.isfinite(source_cell_km) or source_cell_km <= 0:
        raise ValueError("source cell_km must be a finite value greater than zero")
    if policy.recovery_cell_km >= source_cell_km:
        raise ValueError("recovery_cell_km must be strictly finer than the source cell size")
    if isinstance(query_count, bool) or not isinstance(query_count, int) or query_count <= 0:
        raise ValueError("query_count must be a positive integer")

    source_bbox.validate()
    rows, columns = grid_dimensions(source_bbox, source_cell_km)
    if rows < 1 or columns < 1:
        raise ValueError("source grid has zero rows or columns for the recorded cell size")

    lat_edges = build_bin_edges(source_bbox.min_lat, source_bbox.max_lat, rows)
    lon_edges = build_bin_edges(source_bbox.min_lon, source_bbox.max_lon, columns)

    source_grid_estimate = estimate_grid(source_bbox, source_cell_km, query_count)
    bin_bboxes = build_density_bins(
        source_bbox, rows, columns, lat_edges=lat_edges, lon_edges=lon_edges
    )

    counts = [0] * (rows * columns)
    associated = 0
    for latitude, longitude in coordinates:
        row, column = assign_density_bin(
            latitude, longitude, source_bbox, rows, columns,
            lat_edges=lat_edges, lon_edges=lon_edges,
        )
        counts[row * columns + column] += 1
        associated += 1

    bins: list[DensityBin] = []
    for index, bin_bbox in enumerate(bin_bboxes):
        row, column = divmod(index, columns)
        count = counts[index]
        tier = classify_density(count, policy)
        selected = tier != TIER_UNSELECTED
        estimate = estimate_grid(bin_bbox, policy.recovery_cell_km, query_count) if selected else None
        bins.append(
            DensityBin(
                row=row,
                column=column,
                bbox=bin_bbox,
                business_count=count,
                tier=tier,
                selected=selected,
                recovery_estimate=estimate,
            )
        )

    full_uniform = estimate_grid(source_bbox, policy.recovery_cell_km, query_count)
    return RecoveryPlan(
        policy=policy,
        source_run=source_run,
        source_grid_estimate=source_grid_estimate,
        rows=rows,
        columns=columns,
        associated_businesses=associated,
        bins=tuple(bins),
        full_uniform_estimate=full_uniform,
    )


def _bbox_payload(bbox: BoundingBox) -> dict[str, float]:
    return {
        "min_lat": bbox.min_lat,
        "min_lon": bbox.min_lon,
        "max_lat": bbox.max_lat,
        "max_lon": bbox.max_lon,
    }


def _grid_payload(estimate: GridEstimate) -> dict[str, int]:
    return {
        "rows": estimate.rows,
        "columns": estimate.columns,
        "cells": estimate.cells,
        "planned_searches": estimate.searches,
    }


def serialize_recovery_plan(plan: RecoveryPlan) -> str:
    """Deterministic UTF-8 JSON with one trailing newline.

    The canonical payload excludes generation timestamps, absolute paths,
    host, PID and randomness so the same snapshot plus the same arguments
    always produce byte-identical output.
    """
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": PLAN_KIND,
        "policy": {
            "id": plan.policy.policy_id,
            "tier_a_min": plan.policy.tier_a_min,
            "tier_b_min": plan.policy.tier_b_min,
        },
        "source_run": dict(plan.source_run),
        "source_grid_estimate": _grid_payload(plan.source_grid_estimate),
        "binning": {
            "method": BINNING_METHOD,
            "rows": plan.rows,
            "columns": plan.columns,
            "ordering": BIN_ORDERING,
            "edge_rule": EDGE_RULE,
            "associated_businesses": plan.associated_businesses,
        },
        "recovery": {
            "cell_km": plan.policy.recovery_cell_km,
            "same_queries_as_source": True,
            "full_uniform_estimate": _grid_payload(plan.full_uniform_estimate),
        },
        "bins": [
            {
                "row": b.row,
                "column": b.column,
                "bbox": _bbox_payload(b.bbox),
                "business_count": b.business_count,
                "tier": b.tier,
                "selected": b.selected,
                "recovery_estimate": _grid_payload(b.recovery_estimate) if b.recovery_estimate is not None else None,
            }
            for b in plan.bins
        ],
        "summary": plan.summary,
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"


# ---------------------------------------------------------------------------
# Execution-plan parsing and validation (schema v1, recovery-run)
# ---------------------------------------------------------------------------

import re as _re

_RUN_ID_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PLAN_SHA_RE = _re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST_RE = _re.compile(r"^(?!-)[^\s]+@sha256:[0-9a-f]{64}$")

SIX_DECIMAL_MIN_STEP_DEG = 1e-6

_SOURCE_RUN_KEYS = (
    "id", "area_name", "bbox", "cell_km", "depth", "queries", "query_count",
    "scraper_image", "status", "exit_code", "strict_bounds", "resume",
    "started_at", "finished_at", "config_sha256", "config", "completion_claim",
)
_GRID_ESTIMATE_KEYS = ("rows", "columns", "cells", "planned_searches")
_BINNING_KEYS = (
    "method", "rows", "columns", "ordering", "edge_rule", "associated_businesses",
)
_RECOVERY_KEYS = ("cell_km", "same_queries_as_source", "full_uniform_estimate")
_BIN_KEYS = (
    "row", "column", "bbox", "business_count", "tier", "selected", "recovery_estimate",
)
_SUMMARY_KEYS = (
    "tier_a_bins", "tier_b_bins", "unselected_bins", "selected_bins", "total_bins",
    "estimated_recovery_searches", "full_uniform_recovery_searches",
    "search_delta_vs_uniform", "recovery_fraction_of_full",
)
_BBOX_KEYS = ("min_lat", "min_lon", "max_lat", "max_lon")


class PlanRejected(ValueError):
    """Caller/plan rejection for recovery-run (documented exit code 2)."""


@dataclass(frozen=True)
class ExecutionBin:
    row: int
    column: int
    bbox: BoundingBox
    business_count: int
    tier: str
    selected: bool
    planned_searches: int | None


@dataclass(frozen=True)
class ExecutionPlan:
    policy: RecoveryPolicy
    source_run_id: str
    area_name: str
    bbox: BoundingBox
    cell_km: float
    depth: int
    queries: tuple[str, ...]
    scraper_image: str
    started_at: str
    finished_at: str
    exit_code: int | None
    config: dict[str, Any]
    config_sha256: str
    rows: int
    columns: int
    associated_businesses: int
    recovery_cell_km: float
    full_uniform_estimate: GridEstimate
    source_grid_estimate: GridEstimate
    bins: tuple[ExecutionBin, ...]

    @property
    def selected_bins(self) -> tuple[ExecutionBin, ...]:
        return tuple(b for b in self.bins if b.selected)

    @property
    def targeted_searches(self) -> int:
        return sum(b.planned_searches for b in self.bins if b.selected)


def _reject(message: str) -> None:
    raise PlanRejected(message)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _parse_constant_reject(token: str) -> None:
    _reject(f"non-standard JSON constant in plan: {token!r}")


def _exact_keys(obj: Any, keys: tuple[str, ...], where: str) -> None:
    if not isinstance(obj, dict):
        _reject(f"{where} must be a JSON object")
    expected = set(keys)
    actual = set(obj)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unexpected {extra}")
        _reject(f"{where} has wrong field set: {'; '.join(detail)}")


def _require_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        _reject(f"{where} must be a nonempty string")
    return value


def _require_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _reject(f"{where} must be an integer")
    return value


def _require_number(value: Any, where: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(f"{where} must be a number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        # An integer too large for float representation is finite as an
        # integer but cannot be a usable plan number; reject cleanly.
        _reject(f"{where} is an integer too large to represent as a plan number")
    if not finite:
        _reject(f"{where} must be finite")
    return value


def _require_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        _reject(f"{where} must be a boolean")
    return value


def _parse_bbox_object(value: Any, where: str) -> BoundingBox:
    _exact_keys(value, _BBOX_KEYS, where)
    for key in _BBOX_KEYS:
        _require_number(value[key], f"{where}.{key}")
    try:
        bbox = BoundingBox(
            min_lat=value["min_lat"], min_lon=value["min_lon"],
            max_lat=value["max_lat"], max_lon=value["max_lon"],
        )
        bbox.validate()
    except ValueError as exc:
        _reject(f"{where} is not a valid bounding box: {exc}")
    return bbox


def _parse_grid_estimate(value: Any, where: str) -> dict[str, int]:
    _exact_keys(value, _GRID_ESTIMATE_KEYS, where)
    return {key: _require_int(value[key], f"{where}.{key}") for key in _GRID_ESTIMATE_KEYS}


def validate_digest_pinned_image(image: str) -> str:
    if not _IMAGE_DIGEST_RE.fullmatch(image):
        _reject(
            "recovery-run requires a digest-pinned scraper image of the form "
            "name@sha256:<64 lowercase hex>; tag-based plans are evidence-only"
        )
    return image


def parse_execution_plan(data: bytes) -> ExecutionPlan:
    """Strictly parse and semantically validate one frozen schema-v1 plan.

    The caller reads the exact bytes once and passes the same buffer that was
    hashed. Every derived field is recomputed from the frozen planner
    semantics; recorded values are never trusted.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        _reject(f"plan is not valid UTF-8: {exc}")
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_parse_constant_reject,
        )
    except json.JSONDecodeError as exc:
        _reject(f"plan is not valid JSON: {exc}")
    _exact_keys(payload, (
        "schema_version", "kind", "policy", "source_run", "source_grid_estimate",
        "binning", "recovery", "bins", "summary",
    ), "plan")

    if _require_int(payload["schema_version"], "plan.schema_version") != SCHEMA_VERSION:
        _reject(f"unsupported plan schema_version: {payload['schema_version']!r}")
    if _require_str(payload["kind"], "plan.kind") != PLAN_KIND:
        _reject(f"unsupported plan kind: {payload['kind']!r}")

    policy_raw = payload["policy"]
    _exact_keys(policy_raw, ("id", "tier_a_min", "tier_b_min"), "plan.policy")
    # Thresholds are validated here; recovery_cell_km joins the policy only
    # after plan.recovery parses, and the full policy is validated then.
    policy_id = _require_str(policy_raw["id"], "plan.policy.id")
    tier_a_min = _require_int(policy_raw["tier_a_min"], "plan.policy.tier_a_min")
    tier_b_min = _require_int(policy_raw["tier_b_min"], "plan.policy.tier_b_min")
    if isinstance(tier_b_min, bool) or tier_b_min < 1:
        _reject("plan policy is invalid: tier_b_min must be an integer >= 1")
    if tier_a_min <= tier_b_min:
        _reject("plan policy is invalid: tier_a_min must be greater than tier_b_min")
    if len(policy_id) > 128 or not policy_id.strip():
        _reject("plan policy is invalid: policy_id must be 1-128 non-space characters")

    source = payload["source_run"]
    _exact_keys(source, _SOURCE_RUN_KEYS, "plan.source_run")
    source_run_id = _require_str(source["id"], "plan.source_run.id")
    if not _RUN_ID_RE.fullmatch(source_run_id):
        _reject("plan.source_run.id is not a valid run ID")
    area_name = _require_str(source["area_name"], "plan.source_run.area_name")
    source_bbox = _parse_bbox_object(source["bbox"], "plan.source_run.bbox")
    cell_km = _require_number(source["cell_km"], "plan.source_run.cell_km")
    depth = _require_int(source["depth"], "plan.source_run.depth")
    queries_raw = source["queries"]
    if not isinstance(queries_raw, list) or not queries_raw:
        _reject("plan.source_run.queries must be a nonempty list of strings")
    for index, query in enumerate(queries_raw):
        if not isinstance(query, str) or not query:
            _reject(f"plan.source_run.queries[{index}] must be a nonempty string")
    query_count = _require_int(source["query_count"], "plan.source_run.query_count")
    if query_count != len(queries_raw):
        _reject("plan.source_run.query_count does not match queries length")
    scraper_image = _require_str(source["scraper_image"], "plan.source_run.scraper_image")
    if _require_str(source["status"], "plan.source_run.status") != "complete":
        _reject("plan.source_run.status must be 'complete'")
    exit_code = source["exit_code"]
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        _reject("plan.source_run.exit_code must be an integer or null")
    if _require_bool(source["strict_bounds"], "plan.source_run.strict_bounds") is not True:
        _reject("plan.source_run.strict_bounds must be true")
    if _require_bool(source["resume"], "plan.source_run.resume") is not True:
        _reject("plan.source_run.resume must be true")
    started_at = _require_str(source["started_at"], "plan.source_run.started_at")
    finished_at = _require_str(source["finished_at"], "plan.source_run.finished_at")
    config_sha256 = _require_str(source["config_sha256"], "plan.source_run.config_sha256")
    if not _re.fullmatch(r"[0-9a-f]{64}", config_sha256):
        _reject("plan.source_run.config_sha256 must be 64 lowercase hex characters")
    if _require_str(source["completion_claim"], "plan.source_run.completion_claim") != COMPLETION_CLAIM:
        _reject("plan.source_run.completion_claim does not match the schema-v1 constant")

    config = source["config"]
    if not isinstance(config, dict):
        _reject("plan.source_run.config must be a JSON object")
    _exact_keys(config, CONFIG_PROJECTION_FIELDS, "plan.source_run.config")
    config_bbox = _parse_bbox_object(config["bbox"], "plan.source_run.config.bbox")
    config_queries = config["queries"]
    if not isinstance(config_queries, list) or not all(
        isinstance(query, str) and query for query in config_queries
    ):
        _reject("plan.source_run.config.queries must be a list of nonempty strings")
    for field, recorded in (
        ("area_name", area_name), ("image", scraper_image), ("queries", list(queries_raw)),
    ):
        if config[field] != recorded:
            _reject(f"plan.source_run.config.{field} disagrees with the denormalized source field")
    if config_bbox != source_bbox:
        _reject("plan.source_run.config.bbox disagrees with the denormalized source bbox")
    if _require_number(config["cell_km"], "plan.source_run.config.cell_km") != cell_km:
        _reject("plan.source_run.config.cell_km disagrees with the denormalized source cell size")
    if _require_int(config["depth"], "plan.source_run.config.depth") != depth:
        _reject("plan.source_run.config.depth disagrees with the denormalized source depth")
    if config["resume"] is not True or config["strict_bounds"] is not True:
        _reject("plan.source_run.config must record resume=true and strict_bounds=true")
    for numeric_field in ("concurrency", "browser_pool_size", "pages_per_browser", "zoom"):
        _require_int(config[numeric_field], f"plan.source_run.config.{numeric_field}")
    _require_str(config["lang"], "plan.source_run.config.lang")
    if config["proxy_sha256"] is not None and not (
        isinstance(config["proxy_sha256"], str) and _re.fullmatch(r"[0-9a-f]{64}", config["proxy_sha256"])
    ):
        _reject("plan.source_run.config.proxy_sha256 must be null or 64 lowercase hex characters")

    source_grid_raw = _parse_grid_estimate(payload["source_grid_estimate"], "plan.source_grid_estimate")

    binning = payload["binning"]
    _exact_keys(binning, _BINNING_KEYS, "plan.binning")
    if _require_str(binning["method"], "plan.binning.method") != BINNING_METHOD:
        _reject("plan.binning.method does not match the schema-v1 constant")
    if _require_str(binning["ordering"], "plan.binning.ordering") != BIN_ORDERING:
        _reject("plan.binning.ordering does not match the schema-v1 constant")
    if _require_str(binning["edge_rule"], "plan.binning.edge_rule") != EDGE_RULE:
        _reject("plan.binning.edge_rule does not match the schema-v1 constant")
    binning_rows = _require_int(binning["rows"], "plan.binning.rows")
    binning_columns = _require_int(binning["columns"], "plan.binning.columns")
    associated = _require_int(binning["associated_businesses"], "plan.binning.associated_businesses")

    recovery = payload["recovery"]
    _exact_keys(recovery, _RECOVERY_KEYS, "plan.recovery")
    recovery_cell_km = _require_number(recovery["cell_km"], "plan.recovery.cell_km")
    if _require_bool(recovery["same_queries_as_source"], "plan.recovery.same_queries_as_source") is not True:
        _reject("plan.recovery.same_queries_as_source must be true")
    uniform_raw = _parse_grid_estimate(recovery["full_uniform_estimate"], "plan.recovery.full_uniform_estimate")
    policy = RecoveryPolicy(
        policy_id=policy_id,
        tier_a_min=tier_a_min,
        tier_b_min=tier_b_min,
        recovery_cell_km=float(recovery_cell_km),
    )
    try:
        policy.validate()
    except ValueError as exc:
        _reject(f"plan policy is invalid: {exc}")
    if recovery_cell_km >= cell_km:
        _reject("plan recovery cell size must be strictly finer than the source cell size")

    bins_raw = payload["bins"]
    if not isinstance(bins_raw, list):
        _reject("plan.bins must be a list")

    summary = payload["summary"]
    _exact_keys(summary, _SUMMARY_KEYS, "plan.summary")

    # ---- semantic recomputation from frozen planner semantics ----
    expected_rows, expected_columns = grid_dimensions(source_bbox, cell_km)
    if (expected_rows, expected_columns) != (binning_rows, binning_columns):
        _reject("plan.binning rows/columns do not match the source grid")
    if expected_rows < 1 or expected_columns < 1:
        _reject("plan source grid has zero rows or columns")

    expected_source_estimate = estimate_grid(source_bbox, cell_km, query_count)
    if dict(rows=expected_source_estimate.rows, columns=expected_source_estimate.columns,
            cells=expected_source_estimate.cells, planned_searches=expected_source_estimate.searches) != source_grid_raw:
        _reject("plan.source_grid_estimate does not match recomputation")

    lat_edges = build_bin_edges(source_bbox.min_lat, source_bbox.max_lat, expected_rows)
    lon_edges = build_bin_edges(source_bbox.min_lon, source_bbox.max_lon, expected_columns)
    expected_bins: list[ExecutionBin] = []
    if len(bins_raw) != expected_rows * expected_columns:
        _reject("plan.bins length does not match rows * columns")
    total_businesses = 0
    for index, bin_raw in enumerate(bins_raw):
        where = f"plan.bins[{index}]"
        _exact_keys(bin_raw, _BIN_KEYS, where)
        row = _require_int(bin_raw["row"], f"{where}.row")
        column = _require_int(bin_raw["column"], f"{where}.column")
        count = _require_int(bin_raw["business_count"], f"{where}.business_count")
        if count < 0:
            _reject(f"{where}.business_count must be nonnegative")
        if not (0 <= row < expected_rows):
            _reject(f"{where}.row is outside the plan grid (0..{expected_rows - 1})")
        if not (0 <= column < expected_columns):
            _reject(f"{where}.column is outside the plan grid (0..{expected_columns - 1})")
        if index != row * expected_columns + column:
            _reject("plan.bins order must be row-major south-to-north west-to-east")
        expected_bbox = BoundingBox(
            min_lat=lat_edges[row], max_lat=lat_edges[row + 1],
            min_lon=lon_edges[column], max_lon=lon_edges[column + 1],
        )
        recorded_bbox = _parse_bbox_object(bin_raw["bbox"], f"{where}.bbox")
        if recorded_bbox != expected_bbox:
            _reject(f"{where}.bbox does not match the recomputed equal-partition edge")
        tier = classify_density(count, policy)
        if _require_str(bin_raw["tier"], f"{where}.tier") != tier:
            _reject(f"{where}.tier does not match classification from business_count")
        selected = _require_bool(bin_raw["selected"], f"{where}.selected")
        if selected != (tier != TIER_UNSELECTED):
            _reject(f"{where}.selected does not match tier classification")
        estimate = bin_raw["recovery_estimate"]
        if selected:
            expected_estimate = estimate_grid(expected_bbox, policy.recovery_cell_km, query_count)
            parsed_estimate = _parse_grid_estimate(estimate, f"{where}.recovery_estimate")
            if dict(rows=expected_estimate.rows, columns=expected_estimate.columns,
                    cells=expected_estimate.cells,
                    planned_searches=expected_estimate.searches) != parsed_estimate:
                _reject(f"{where}.recovery_estimate does not match recomputation")
            planned = expected_estimate.searches
        else:
            if estimate is not None:
                _reject(f"{where}.recovery_estimate must be null for unselected bins")
            planned = None
        total_businesses += count
        expected_bins.append(ExecutionBin(
            row=row, column=column, bbox=expected_bbox, business_count=count,
            tier=tier, selected=selected, planned_searches=planned,
        ))
    if total_businesses != associated:
        _reject("plan.binning.associated_businesses does not equal the sum of bin business counts")

    expected_uniform = estimate_grid(source_bbox, policy.recovery_cell_km, query_count)
    if dict(rows=expected_uniform.rows, columns=expected_uniform.columns,
            cells=expected_uniform.cells, planned_searches=expected_uniform.searches) != uniform_raw:
        _reject("plan.recovery.full_uniform_estimate does not match recomputation")

    targeted = sum(b.planned_searches for b in expected_bins if b.selected)
    expected_summary = {
        "tier_a_bins": sum(1 for b in expected_bins if b.tier == TIER_A),
        "tier_b_bins": sum(1 for b in expected_bins if b.tier == TIER_B),
        "unselected_bins": sum(1 for b in expected_bins if b.tier == TIER_UNSELECTED),
        "selected_bins": sum(1 for b in expected_bins if b.selected),
        "total_bins": len(expected_bins),
        "estimated_recovery_searches": targeted,
        "full_uniform_recovery_searches": expected_uniform.searches,
        "search_delta_vs_uniform": targeted - expected_uniform.searches,
        "recovery_fraction_of_full": (targeted / expected_uniform.searches) if expected_uniform.searches else 0.0,
    }
    for key in _SUMMARY_KEYS:
        if key == "recovery_fraction_of_full":
            recorded = _require_number(summary[key], f"plan.summary.{key}")
        else:
            recorded = _require_int(summary[key], f"plan.summary.{key}")
        if recorded != expected_summary[key]:
            _reject(f"plan.summary.{key} does not match recomputation")

    return ExecutionPlan(
        policy=policy,
        source_run_id=source_run_id,
        area_name=area_name,
        bbox=source_bbox,
        cell_km=float(cell_km),
        depth=depth,
        queries=tuple(queries_raw),
        scraper_image=scraper_image,
        started_at=started_at,
        finished_at=finished_at,
        exit_code=exit_code,
        config=config,
        config_sha256=config_sha256,
        rows=expected_rows,
        columns=expected_columns,
        associated_businesses=associated,
        recovery_cell_km=policy.recovery_cell_km,
        full_uniform_estimate=expected_uniform,
        source_grid_estimate=expected_source_estimate,
        bins=tuple(expected_bins),
    )


def validate_child_coordinate_precision(bbox: BoundingBox, cell_km: float) -> None:
    """Reject child geometry whose six-decimal resume IDs could collide.

    Upstream resume identities format coordinates to six decimal places, so a
    grid step below 1e-6 degree on an axis with more than one origin can make
    distinct origins collide after formatting. The authoritative grid step
    calculation decides; this guard never changes planner selection.
    """
    from .grid import grid_steps

    lat_step, lon_step = grid_steps(bbox, cell_km)
    rows, columns = grid_dimensions(bbox, cell_km)
    if rows > 1 and lat_step < SIX_DECIMAL_MIN_STEP_DEG:
        raise PlanRejected(
            "selected recovery bin cannot produce collision-free six-decimal "
            f"resume coordinates: latitude step {lat_step!r} < 1e-6 with {rows} rows"
        )
    if columns > 1 and abs(lon_step) < SIX_DECIMAL_MIN_STEP_DEG:
        raise PlanRejected(
            "selected recovery bin cannot produce collision-free six-decimal "
            f"resume coordinates: longitude step {lon_step!r} < 1e-6 with {columns} columns"
        )
