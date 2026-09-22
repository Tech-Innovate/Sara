from __future__ import annotations

import math
from dataclasses import dataclass

from .config import BoundingBox

KM_PER_DEGREE_LAT = 111.32


@dataclass(frozen=True)
class GridEstimate:
    rows: int
    columns: int
    cells: int
    searches: int


def estimate_grid(bbox: BoundingBox, cell_km: float, query_count: int) -> GridEstimate:
    if cell_km <= 0:
        raise ValueError("cell_km must be greater than zero")
    if query_count <= 0:
        raise ValueError("query_count must be greater than zero")

    bbox.validate()
    lat_km = (bbox.max_lat - bbox.min_lat) * KM_PER_DEGREE_LAT
    midpoint = math.radians((bbox.min_lat + bbox.max_lat) / 2)
    lon_km_per_degree = KM_PER_DEGREE_LAT * max(math.cos(midpoint), 1e-6)
    lon_km = (bbox.max_lon - bbox.min_lon) * lon_km_per_degree

    rows = max(1, math.ceil(lat_km / cell_km))
    columns = max(1, math.ceil(lon_km / cell_km))
    cells = rows * columns
    return GridEstimate(rows=rows, columns=columns, cells=cells, searches=cells * query_count)
