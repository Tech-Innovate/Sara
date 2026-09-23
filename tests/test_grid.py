import pytest
from sara.config import BoundingBox
from sara.grid import estimate_grid, grid_dimensions, iter_grid_origins


def test_estimate_grid_scales_by_query_count():
    bbox = BoundingBox(21.45, 39.10, 21.55, 39.20)
    one = estimate_grid(bbox, 1.0, 1)
    three = estimate_grid(bbox, 1.0, 3)
    assert one.cells > 0
    assert three.searches == one.cells * 3


def test_estimate_grid_matches_upstream_half_step_origin_count():
    bbox = BoundingBox(0, 0, 1, 1)

    estimate = estimate_grid(bbox, 1.0, 1)

    assert estimate.rows == 111
    assert estimate.columns == 111
    assert estimate.cells == 12321
    assert estimate.searches == 12321


def test_estimate_grid_can_report_zero_cells_for_too_small_box():
    # Upstream starts the first origin half a cell from the minimum edge.
    bbox = BoundingBox(0, 0, 0.001, 0.001)

    estimate = estimate_grid(bbox, 1.0, 1)

    assert estimate.rows == 0
    assert estimate.columns == 0
    assert estimate.cells == 0


# ---- V2-SR01: grid non-progress guard regressions ----


def _reference_origin_count(minimum: float, maximum: float, step: float) -> int:
    """The original upstream-compatible counting loop, verbatim."""
    count = 0
    value = minimum + step / 2
    while value < maximum:
        count += 1
        value += step
    return count


def test_tiny_finite_cell_size_raises_instead_of_hanging():
    bbox = BoundingBox(21.5, 39.1, 21.6, 39.2)
    for operation in (
        lambda: estimate_grid(bbox, 1e-13, 1),
        lambda: grid_dimensions(bbox, 1e-13),
        lambda: list(iter_grid_origins(bbox, 1e-13)),
    ):
        with pytest.raises(ValueError, match="too small to advance"):
            operation()


@pytest.mark.parametrize("cell_km", [2.0, 1.0, 0.5])
def test_ordinary_cell_sizes_keep_upstream_compatible_counts(cell_km):
    bbox = BoundingBox(21.45, 39.10, 21.55, 39.20)
    rows, columns = grid_dimensions(bbox, cell_km)
    estimate = estimate_grid(bbox, cell_km, 1)

    km_per_deg_lat = 111.32
    import math as _math

    lat_step = cell_km / km_per_deg_lat
    midpoint = _math.radians((bbox.min_lat + bbox.max_lat) / 2)
    cos_midpoint = _math.cos(midpoint)
    lon_step = cell_km / (km_per_deg_lat * cos_midpoint)

    assert rows == _reference_origin_count(bbox.min_lat, bbox.max_lat, lat_step)
    assert columns == _reference_origin_count(bbox.min_lon, bbox.max_lon, lon_step)
    assert estimate.rows == rows
    assert estimate.columns == columns
    assert estimate.cells == rows * columns


def test_negative_longitude_magnitude_is_guarded_too():
    bbox = BoundingBox(21.5, -39.2, 21.6, -39.1)
    with pytest.raises(ValueError, match="too small to advance"):
        grid_dimensions(bbox, 1e-13)
    # Normal cells still work at the same magnitudes.
    rows, columns = grid_dimensions(bbox, 1.0)
    assert rows > 0 and columns > 0
