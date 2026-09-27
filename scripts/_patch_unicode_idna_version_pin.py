from pathlib import Path

path = Path("tests/test_website_production_hardening.py")
text = path.read_text(encoding="utf-8")
old = '    assert COLLECTOR_VERSION == "3"\n'
new = '    assert COLLECTOR_VERSION == "4"\n'
if text.count(old) != 1:
    raise SystemExit(f"expected one stale collector-version assertion, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
