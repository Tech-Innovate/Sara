from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

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


def assign_density_bin(
    latitude: float,
    longitude: float,
    bbox: BoundingBox,
    rows: int,
    columns: int,
) -> tuple[int, int]:
    """Map a coordinate to its density bin.

    Rows run south to north, columns west to east. The exact north/east
    maximum belongs to the final row/column. Nonfinite and out-of-bounds
    coordinates are errors; they are never silently clamped.
    """
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ValueError(f"nonfinite coordinate: ({latitude!r}, {longitude!r})")
    if not bbox.contains(latitude, longitude):
        raise ValueError(f"coordinate ({latitude!r}, {longitude!r}) lies outside the source bounding box")
    row = math.floor((latitude - bbox.min_lat) / (bbox.max_lat - bbox.min_lat) * rows)
    column = math.floor((longitude - bbox.min_lon) / (bbox.max_lon - bbox.min_lon) * columns)
    if row >= rows:
        row = rows - 1
    if column >= columns:
        column = columns - 1
    return int(row), int(column)


def build_density_bins(bbox: BoundingBox, rows: int, columns: int) -> list[BoundingBox]:
    """Evenly partition the bbox into rows x columns bin bboxes.

    Order is deterministic: row ascending (south to north), then column
    ascending (west to east).
    """
    if rows < 1 or columns < 1:
        raise ValueError("density grid must have at least one row and one column")
    lat_height = (bbox.max_lat - bbox.min_lat) / rows
    lon_width = (bbox.max_lon - bbox.min_lon) / columns
    bins: list[BoundingBox] = []
    for row in range(rows):
        for column in range(columns):
            bins.append(
                BoundingBox(
                    min_lat=bbox.min_lat + row * lat_height,
                    min_lon=bbox.min_lon + column * lon_width,
                    max_lat=bbox.min_lat + (row + 1) * lat_height,
                    max_lon=bbox.min_lon + (column + 1) * lon_width,
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

    source_grid_estimate = estimate_grid(source_bbox, source_cell_km, query_count)
    bin_bboxes = build_density_bins(source_bbox, rows, columns)

    counts = [0] * (rows * columns)
    associated = 0
    for latitude, longitude in coordinates:
        row, column = assign_density_bin(latitude, longitude, source_bbox, rows, columns)
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
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
