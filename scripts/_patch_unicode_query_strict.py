from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"expected one match in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/sara/website/parser.py",
    "            for key, item in parse_qsl(parsed.query, keep_blank_values=True)\n",
    "            for key, item in parse_qsl(\n                parsed.query,\n                keep_blank_values=True,\n                encoding=\"utf-8\",\n                errors=\"strict\",\n            )\n",
)

path = Path("tests/test_website_parser.py")
text = path.read_text(encoding="utf-8")
marker = "def test_invalid_percent_encoded_query_utf8_fails_closed"
if marker not in text:
    text += '''\n\ndef test_invalid_percent_encoded_query_utf8_fails_closed() -> None:\n    assert normalize_http_url("https://example.com/?q=%FF") is None\n    assert normalize_http_url("https://example.com/?q=%C3%28") is None\n'''
    path.write_text(text, encoding="utf-8")
