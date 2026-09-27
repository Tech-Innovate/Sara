from pathlib import Path

path = Path("tests/test_website_production_hardening.py")
text = path.read_text(encoding="utf-8")
replacements = (
    ('    assert COLLECTOR_VERSION == "3"\n', '    assert COLLECTOR_VERSION == "4"\n'),
    ('    assert row[0] == "3"\n', '    assert row[0] == "4"\n'),
)
for old, new in replacements:
    if text.count(old) != 1:
        raise SystemExit(f"expected one stale collector-version assertion {old!r}, found {text.count(old)}")
    text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
