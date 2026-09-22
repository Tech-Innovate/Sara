from sara.config import BoundingBox
from sara.grid import estimate_grid


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
