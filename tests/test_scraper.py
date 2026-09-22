import os
import stat
from pathlib import Path

import pytest

from sara.config import AreaConfig, BoundingBox
from sara.scraper import ScrapeOptions, build_docker_command


def area():
    return AreaConfig("x", BoundingBox(0, 0, 1, 1))


def test_grid_zoom_zero_is_rejected():
    with pytest.raises(ValueError, match="1 and 21"):
        ScrapeOptions(zoom=0).validate()


def test_non_finite_cell_size_is_rejected():
    with pytest.raises(ValueError, match="finite"):
        ScrapeOptions(cell_km=float("nan")).validate()


def test_docker_command_mounts_query_snapshot_and_disables_telemetry(tmp_path):
    queries = tmp_path / "queries.txt"
    queries.write_text("dentist\n", encoding="utf-8")
    output = tmp_path / "out" / "results.jsonl"

    command = build_docker_command(
        area=area(),
        queries_file=queries,
        output_file=output,
        options=ScrapeOptions(),
    )

    assert command[:5] == ["docker", "run", "--rm", "-e", "DISABLE_TELEMETRY=1"]
    assert f"{queries.resolve()}:/queries.txt:ro" in command
    assert "-grid-bbox" in command
    assert "-resume" in command
    assert output.exists()
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_dry_run_command_preparation_does_not_create_output(tmp_path):
    queries = tmp_path / "missing-run-snapshot.txt"
    output = tmp_path / "out" / "results.jsonl"

    build_docker_command(
        area=area(),
        queries_file=queries,
        output_file=output,
        options=ScrapeOptions(),
        prepare_paths=False,
    )

    assert not output.exists()
    assert not output.parent.exists()
