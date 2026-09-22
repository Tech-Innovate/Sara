from __future__ import annotations

import math
from dataclasses import dataclass

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
    if not math.isfinite(cell_km) or cell_km <= 0:
        raise ValueError("cell_km must be a finite value greater than zero")
    if query_count <= 0:
        raise ValueError("query_count must be greater than zero")

    bbox.validate()
    lat_step = cell_km / KM_PER_DEGREE_LAT
    midpoint = math.radians((bbox.min_lat + bbox.max_lat) / 2)
    cos_midpoint = math.cos(midpoint)
    if abs(cos_midpoint) < MIN_COS_LATITUDE:
        cos_midpoint = -MIN_COS_LATITUDE if cos_midpoint < 0 else MIN_COS_LATITUDE
    lon_step = cell_km / (KM_PER_DEGREE_LAT * cos_midpoint)

    # Mirror upstream GenerateCells exactly: origins start half a cell from the
    # minimum edge and are emitted only while the center remains below max.
    rows = _count_origins(bbox.min_lat, bbox.max_lat, lat_step)
    columns = _count_origins(bbox.min_lon, bbox.max_lon, lon_step)
    cells = rows * columns
    return GridEstimate(rows=rows, columns=columns, cells=cells, searches=cells * query_count)


def _count_origins(minimum: float, maximum: float, step: float) -> int:
    count = 0
    value = minimum + step / 2
    while value < maximum:
        count += 1
        value += step
    return count
