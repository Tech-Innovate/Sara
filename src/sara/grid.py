from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

from .config import BoundingBox

KM_PER_DEGREE_LAT = 111.32
MIN_COS_LATITUDE = 1e-6


@dataclass(frozen=True)
class GridEstimate:
    rows: int
    columns: int
    cells: int
    searches: int


def estimate_grid(bbox: BoundingBox, cell_km: float, query_count: int) -> GridEstimate:
    if query_count <= 0:
        raise ValueError("query_count must be greater than zero")

    rows, columns = grid_dimensions(bbox, cell_km)
    cells = rows * columns
    return GridEstimate(rows=rows, columns=columns, cells=cells, searches=cells * query_count)


def grid_dimensions(bbox: BoundingBox, cell_km: float) -> tuple[int, int]:
    lat_step, lon_step = _grid_steps(bbox, cell_km)
    rows = _count_origins(bbox.min_lat, bbox.max_lat, lat_step)
    columns = _count_origins(bbox.min_lon, bbox.max_lon, lon_step)
    return rows, columns


def iter_grid_origins(bbox: BoundingBox, cell_km: float) -> Iterator[tuple[float, float]]:
    """Yield the same half-cell grid origins as upstream v1.18.1 GenerateCells."""
    lat_step, lon_step = _grid_steps(bbox, cell_km)
    lat = bbox.min_lat + lat_step / 2
    while lat < bbox.max_lat:
        lon = bbox.min_lon + lon_step / 2
        while lon < bbox.max_lon:
            yield lat, lon
            lon += lon_step
        lat += lat_step


def _grid_steps(bbox: BoundingBox, cell_km: float) -> tuple[float, float]:
    if not math.isfinite(cell_km) or cell_km <= 0:
        raise ValueError("cell_km must be a finite value greater than zero")

    bbox.validate()
    lat_step = cell_km / KM_PER_DEGREE_LAT
    midpoint = math.radians((bbox.min_lat + bbox.max_lat) / 2)
    cos_midpoint = math.cos(midpoint)
    if abs(cos_midpoint) < MIN_COS_LATITUDE:
        cos_midpoint = -MIN_COS_LATITUDE if cos_midpoint < 0 else MIN_COS_LATITUDE
    lon_step = cell_km / (KM_PER_DEGREE_LAT * cos_midpoint)
    # A step below the floating-point resolution at this coordinate magnitude
    # makes the half-step/full-step additions stop advancing, which would
    # loop forever in counting and origin iteration. Fail closed instead.
    _assert_step_progress(bbox.min_lat, lat_step, "latitude")
    _assert_step_progress(bbox.max_lat, lat_step, "latitude")
    _assert_step_progress(bbox.min_lon, lon_step, "longitude")
    _assert_step_progress(bbox.max_lon, lon_step, "longitude")
    return lat_step, lon_step


def _assert_step_progress(value: float, step: float, axis: str) -> None:
    if value + step / 2 <= value:
        raise ValueError(
            f"cell size is too small to advance the {axis} grid at this coordinate magnitude"
        )


def _count_origins(minimum: float, maximum: float, step: float) -> int:
    # Mirror upstream GenerateCells exactly: origins start half a cell from the
    # minimum edge and are emitted only while the center remains below max.
    count = 0
    value = minimum + step / 2
    while value < maximum:
        count += 1
        value += step
    return count
