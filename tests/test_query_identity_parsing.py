from sara.scraper import _parse_upstream_query_identity


def test_query_id_parser_preserves_c0_separators_that_go_does_not_trim():
    query_text, query_id = _parse_upstream_query_identity("restaurant#!#\x1cid\x1f")

    assert query_text == "restaurant"
    assert query_id == "\x1cid\x1f"


def test_query_id_parser_trims_unicode_space_recognized_by_go():
    query_text, query_id = _parse_upstream_query_identity("\u00a0restaurant\u00a0#!#\u3000custom-id\u3000")

    assert query_text == "restaurant"
    assert query_id == "custom-id"
