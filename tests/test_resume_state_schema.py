import json

import pytest

from sara.scraper import load_resume_completed_input_ids


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_resume_state_version_requires_integer_one(tmp_path, version):
    output = tmp_path / "results.jsonl"
    output.write_text("", encoding="utf-8")
    state = tmp_path / "results.jsonl.resume.json"
    state.write_text(
        json.dumps({"version": version, "completed_inputs": []}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unsupported resume state version"):
        load_resume_completed_input_ids(output, "image")


def test_resume_state_rejects_duplicate_completed_ids(tmp_path):
    output = tmp_path / "results.jsonl"
    output.write_text("", encoding="utf-8")
    state = tmp_path / "results.jsonl.resume.json"
    state.write_text(
        json.dumps({"version": 1, "completed_inputs": ["resume:x", "resume:x"]}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="duplicate IDs"):
        load_resume_completed_input_ids(output, "image")
