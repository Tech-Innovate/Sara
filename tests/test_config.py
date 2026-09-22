from sara.config import load_queries, write_query_snapshot


def test_query_loader_removes_comments_blanks_and_exact_duplicates(tmp_path):
    source = tmp_path / "queries.txt"
    source.write_text("# comment\n\ndentist\nrestaurant\ndentist\n", encoding="utf-8")

    queries = load_queries(source)

    assert queries == ["dentist", "restaurant"]


def test_query_snapshot_contains_only_executable_queries(tmp_path):
    destination = tmp_path / "run" / "queries.txt"
    write_query_snapshot(destination, ["dentist", "restaurant"])

    assert destination.read_text(encoding="utf-8") == "dentist\nrestaurant\n"
