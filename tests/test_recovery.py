
import pytest

from sara.config import BoundingBox
from sara.grid import estimate_grid
from sara.recovery import (
    RecoveryPolicy,
    assign_density_bin,
    build_density_bins,
    build_recovery_plan,
    classify_density,
    serialize_recovery_plan,
)

BOX = BoundingBox(0.0, 0.0, 0.5, 1.0)  # non-square: 2 rows x 2 cols at this size


def policy(**overrides) -> RecoveryPolicy:
    values = dict(policy_id="pilot-test", tier_a_min=15, tier_b_min=11, recovery_cell_km=1.0)
    values.update(overrides)
    return RecoveryPolicy(**values)


class TestAssignDensityBin:
    def test_minimum_corner_belongs_to_first_bin(self):
        assert assign_density_bin(0.0, 0.0, BOX, 2, 2) == (0, 0)

    def test_internal_boundary_belongs_to_higher_bin(self):
        # Exact mid-latitude is the floor edge of the northern row.
        assert assign_density_bin(0.25, 0.5, BOX, 2, 2) == (1, 1)
        assert assign_density_bin(0.25, 0.49, BOX, 2, 2) == (1, 0)

    def test_exact_maximum_corner_belongs_to_final_bin(self):
        assert assign_density_bin(0.5, 1.0, BOX, 2, 2) == (1, 1)

    def test_non_square_grid(self):
        assert assign_density_bin(0.1, 0.9, BOX, 2, 2) == (0, 1)
        tall = BoundingBox(0.0, 0.0, 0.6, 0.2)
        assert assign_density_bin(0.5, 0.1, tall, 3, 1) == (2, 0)

    @pytest.mark.parametrize("latitude,longitude", [
        (float("nan"), 0.5),
        (0.5, float("nan")),
        (float("inf"), 0.5),
        (0.5, float("-inf")),
    ])
    def test_nonfinite_coordinates_are_rejected(self, latitude, longitude):
        with pytest.raises(ValueError, match="nonfinite"):
            assign_density_bin(latitude, longitude, BOX, 2, 2)

    @pytest.mark.parametrize("latitude,longitude", [
        (-0.0001, 0.5),
        (0.5001, 0.5),
        (0.25, -0.0001),
        (0.25, 1.0001),
    ])
    def test_outside_coordinates_are_rejected_not_clamped(self, latitude, longitude):
        with pytest.raises(ValueError, match="outside"):
            assign_density_bin(latitude, longitude, BOX, 2, 2)


class TestBuildDensityBins:
    def test_deterministic_south_to_north_west_to_east_order(self):
        bins = build_density_bins(BOX, 2, 2)
        assert [(b.min_lat, b.min_lon) for b in bins] == [
            (0.0, 0.0), (0.0, 0.5),
            (0.25, 0.0), (0.25, 0.5),
        ]

    def test_rejects_zero_sized_grid(self):
        with pytest.raises(ValueError):
            build_density_bins(BOX, 0, 2)
        with pytest.raises(ValueError):
            build_density_bins(BOX, 2, 0)


class TestClassifyDensity:
    @pytest.mark.parametrize("count,tier", [
        (0, "unselected"),
        (10, "unselected"),
        (11, "B"),
        (14, "B"),
        (15, "A"),
        (16, "A"),
        (99, "A"),
    ])
    def test_boundaries(self, count, tier):
        assert classify_density(count, policy()) == tier

    def test_invalid_threshold_relations(self):
        with pytest.raises(ValueError):
            policy(tier_b_min=0).validate()
        with pytest.raises(ValueError):
            policy(tier_a_min=11).validate()
        with pytest.raises(ValueError):
            policy(tier_a_min=10).validate()

    def test_invalid_policy_id_and_cell(self):
        with pytest.raises(ValueError):
            policy(policy_id=" ").validate()
        with pytest.raises(ValueError):
            policy(policy_id="x" * 129).validate()
        with pytest.raises(ValueError):
            policy(recovery_cell_km=0.0).validate()
        with pytest.raises(ValueError):
            policy(recovery_cell_km=float("nan")).validate()


class TestBuildRecoveryPlan:
    def _source_run(self):
        return {
            "id": "base-run",
            "area_name": "x",
            "bbox": {"min_lat": 0.0, "min_lon": 0.0, "max_lat": 0.05, "max_lon": 0.05},
            "cell_km": 2.0,
            "depth": 5,
            "queries": ["restaurant"],
            "query_count": 1,
            "scraper_image": "img",
            "status": "complete",
            "exit_code": 0,
            "strict_bounds": True,
            "resume": True,
            "started_at": "t0",
            "finished_at": "t1",
            "config_sha256": "0" * 64,
            "completion_claim": "recorded_complete_not_reverified_by_recovery_plan",
        }

    def _plan(self, *, recovery_cell_km=1.0, tier_a=15, tier_b=11, coords=None):
        source_bbox = BoundingBox(0.0, 0.0, 0.05, 0.05)
        if coords is None:
            # 15 businesses in bin (0,0), 11 in bin (1,1): tiers A and B.
            coords = [(0.001, 0.001)] * 15 + [(0.03, 0.03)] * 11
        return build_recovery_plan(
            policy=RecoveryPolicy("pilot-test", tier_a, tier_b, recovery_cell_km),
            source_run=self._source_run(),
            source_bbox=source_bbox,
            source_cell_km=2.0,
            query_count=1,
            coordinates=coords,
        )

    def test_tier_counts_and_all_bins_emitted(self):
        plan = self._plan()
        assert plan.rows == 3 and plan.columns == 3
        assert len(plan.bins) == 9
        assert plan.summary["tier_a_bins"] == 1
        assert plan.summary["tier_b_bins"] == 1
        assert plan.summary["unselected_bins"] == 7
        assert plan.summary["selected_bins"] == 2
        assert plan.associated_businesses == 26
        # Unselected bins are still emitted with a null estimate.
        unselected = [b for b in plan.bins if not b.selected]
        assert len(unselected) == 7
        assert all(b.recovery_estimate is None for b in unselected)

    def test_per_bin_estimate_is_real_estimate_not_times_four(self):
        # Recovery 1.5 km on ~2 km bins: each selected bin plans 1x1 = 1
        # search, so the total cannot come from selected_bins * 4.
        plan = self._plan(recovery_cell_km=1.5)
        selected = [b for b in plan.bins if b.selected]
        assert selected
        assert all(b.recovery_estimate.searches == 1 for b in selected)
        assert plan.summary["estimated_recovery_searches"] == len(selected)
        assert plan.summary["estimated_recovery_searches"] != 4 * len(selected)

    def test_signed_delta_can_be_positive(self):
        # Recovery 1.2 km with every bin selected: nine independent anchors
        # plan 9 * 4 = 36 searches while one uniform pass plans 5x5 = 25.
        coords = [
            (0.008, 0.008), (0.008, 0.025), (0.008, 0.042),
            (0.025, 0.008), (0.025, 0.025), (0.025, 0.042),
            (0.042, 0.008), (0.042, 0.025), (0.042, 0.042),
        ]
        plan = self._plan(recovery_cell_km=1.2, tier_a=2, tier_b=1, coords=coords)
        assert plan.summary["selected_bins"] == 9
        assert plan.summary["full_uniform_recovery_searches"] == 25
        assert plan.summary["estimated_recovery_searches"] == 36
        assert plan.summary["search_delta_vs_uniform"] == 11
        assert plan.summary["recovery_fraction_of_full"] > 1.0

    def test_signed_delta_is_negative_when_targeting_saves_searches(self):
        plan = self._plan(recovery_cell_km=1.5)
        assert plan.summary["search_delta_vs_uniform"] < 0

    def test_full_uniform_estimate_scales_by_source_query_count(self):
        plan = self._plan()
        uniform_cells = plan.full_uniform_estimate.cells
        assert plan.full_uniform_estimate.searches == uniform_cells * 1
        # The same snapshot with two recorded queries doubles uniform work.
        plan2 = build_recovery_plan(
            policy=RecoveryPolicy("p", 15, 11, 1.0),
            source_run=self._source_run(),
            source_bbox=BoundingBox(0.0, 0.0, 0.05, 0.05),
            source_cell_km=2.0,
            query_count=2,
            coordinates=[(0.001, 0.001)] * 15 + [(0.03, 0.03)] * 11,
        )
        assert plan2.full_uniform_estimate.searches == uniform_cells * 2

    def test_recovery_cell_must_be_finer_than_source(self):
        with pytest.raises(ValueError, match="strictly finer"):
            self._plan(recovery_cell_km=2.0)
        with pytest.raises(ValueError, match="strictly finer"):
            self._plan(recovery_cell_km=3.0)

    def test_outside_coordinate_fails_the_whole_plan(self):
        with pytest.raises(ValueError, match="outside"):
            self._plan(coords=[(0.9, 0.9)])

    def test_nonfinite_coordinate_fails_the_whole_plan(self):
        with pytest.raises(ValueError, match="nonfinite"):
            self._plan(coords=[(float("nan"), 0.001)])


class TestSerialize:
    def _plan(self):
        source_run = {
            "id": "base-run",
            "completion_claim": "recorded_complete_not_reverified_by_recovery_plan",
        }
        return build_recovery_plan(
            policy=RecoveryPolicy("pilot-test", 15, 11, 1.0),
            source_run=source_run,
            source_bbox=BoundingBox(0.0, 0.0, 0.05, 0.05),
            source_cell_km=2.0,
            query_count=1,
            coordinates=[(0.001, 0.001)] * 15 + [(0.03, 0.03)] * 11,
        )

    def test_byte_identical_for_same_inputs(self):
        assert serialize_recovery_plan(self._plan()) == serialize_recovery_plan(self._plan())

    def test_schema_fields_and_reconciled_metric_names(self):
        import json

        payload = json.loads(serialize_recovery_plan(self._plan()))
        assert payload["schema_version"] == 1
        assert payload["kind"] == "sara.recovery_plan"
        assert "search_delta_vs_uniform" in payload["summary"]
        assert "estimated_search_savings" not in payload["summary"]
        assert "recovery_fraction_of_full" in payload["summary"]
        assert "source_grid_estimate" in payload
        assert payload["source_grid_estimate"]["planned_searches"] == 9
        assert payload["binning"]["associated_businesses"] == 26
        assert len(payload["bins"]) == 9
        assert payload["bins"][0]["selected"] is True
        assert payload["bins"][8]["selected"] is False

    def test_trailing_newline_and_sorted_keys(self):
        text = serialize_recovery_plan(self._plan())
        assert text.endswith("}\n")
        assert text.index('"binning"') < text.index('"bins"') < text.index('"kind"') < text.index('"policy"')

    def test_no_incidental_runtime_metadata(self):
        text = serialize_recovery_plan(self._plan())
        for banned in ("timestamp", "generated_at", "hostname", " pid", "db_path", "output_path", "uuid"):
            assert banned not in text.lower()
