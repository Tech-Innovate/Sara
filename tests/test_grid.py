from sara.config import BoundingBox
from sara.grid import estimate_grid


def test_estimate_grid_scales_by_query_count():
    bbox = BoundingBox(21.45, 39.10, 21.55, 39.20)
    one = estimate_grid(bbox, 1.0, 1)
    three = estimate_grid(bbox, 1.0, 3)
    assert one.cells > 0
    assert three.searches == one.cells * 3
