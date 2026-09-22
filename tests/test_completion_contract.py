import pytest

import sara.scraper as scraper
from sara.config import AreaConfig, BoundingBox
from sara.scraper import expected_resume_input_ids


def test_expected_resume_ids_cover_multi_cell_custom_id_and_arabic_utf8():
    area = AreaConfig("multi", BoundingBox(21.52, 39.17, 21.55, 39.185))

    actual = set(expected_resume_input_ids(
        area,
        ["restaurant", "مطعم#!#arabic-food"],
        2.0,
    ))

    assert actual == {
        "resume:9467a14e5b28588a84acfd173c50d808ee059cc5e55ab076080420722a8658a7",
        "resume:053cfccfcd6a59c9f01196b30d85a105e21cb10cb1e7bac1e24829cbd6356c36",
        "resume:5c7135d50e01bad2b0448c0076d833eb5524e16ade86a4d7ab57171bc1aa7c36",
        "resume:7cd51b791e50e93ae03cda28ccc1cf27c556eeca0d65b56929e3644325671819",
    }


def test_duplicate_custom_query_ids_are_rejected_before_materialization():
    area = AreaConfig("smoke", BoundingBox(21.52, 39.17, 21.535, 39.185))

    with pytest.raises(ValueError, match="duplicate resume identities"):
        expected_resume_input_ids(
            area,
            ["restaurant#!#same", "cafe#!#same"],
            2.0,
        )


def test_expected_ids_are_not_materialized_by_length_check(monkeypatch):
    area = AreaConfig("smoke", BoundingBox(21.52, 39.17, 21.535, 39.185))

    def forbidden_materialization(*_args, **_kwargs):
        raise AssertionError("expected IDs materialized before comparison")
        yield  # pragma: no cover

    monkeypatch.setattr(scraper, "_iter_expected_resume_input_ids", forbidden_materialization)

    expected = expected_resume_input_ids(area, ["restaurant"], 2.0)
    assert len(expected) == 1


def test_expected_ids_materialize_once_when_exact_comparison_begins(monkeypatch):
    area = AreaConfig("smoke", BoundingBox(21.52, 39.17, 21.535, 39.185))
    expected_id = "resume:9467a14e5b28588a84acfd173c50d808ee059cc5e55ab076080420722a8658a7"
    calls = []

    def fake_ids(*_args, **_kwargs):
        calls.append(True)
        yield expected_id

    monkeypatch.setattr(scraper, "_iter_expected_resume_input_ids", fake_ids)
    expected = expected_resume_input_ids(area, ["restaurant"], 2.0)

    assert {expected_id} - expected == set()
    assert expected - {expected_id} == set()
    assert calls == [True]
