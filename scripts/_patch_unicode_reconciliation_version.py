from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"expected one match in {path}: {old!r}; found {text.count(old)}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/sara/website/model.py",
    'RECONCILIATION_VERSION = "official-web-v2"\n',
    'RECONCILIATION_VERSION = "official-web-v3"\n',
)
replace_once(
    "tests/test_website_url_convergence.py",
    '    assert current[2] == "official-web-v2" == RECONCILIATION_VERSION\n',
    '    assert current[2] == "official-web-v3" == RECONCILIATION_VERSION\n',
)
replace_once(
    "docs/website-operational-validation.md",
    "This reconciliation behavior is versioned as `official-web-v2`; the website collector version remains unchanged because request/extraction behavior did not change.\n",
    "The evidence-backed alias behavior was introduced as `official-web-v2`. Unicode/IDNA URL canonicalization later changed shared website-value equivalence semantics and therefore advances current reconciliation provenance to `official-web-v3`.\n",
)
replace_once(
    "docs/website-operational-validation.md",
    "Official-site URLs are canonicalized to an ASCII network representation before DNS, TLS, robots, SSRF checks, origin comparison, and HTTP request serialization. Unicode hostname labels are IDNA-encoded; raw Unicode path/query characters are UTF-8 percent-encoded. Invalid Unicode/IDNA fails closed. This transport-normalization change is versioned with website collector version `4`; website reconciliation remains `official-web-v2`.",
    "Official-site URLs are canonicalized to an ASCII network representation before DNS, TLS, robots, SSRF checks, origin comparison, and HTTP request serialization. Unicode hostname labels are IDNA-encoded; raw Unicode path/query characters are UTF-8 percent-encoded. Invalid Unicode/IDNA fails closed. This transport-normalization change is versioned with website collector version `4`; because the shared normalizer also changes website-value equivalence, reconciliation provenance advances to `official-web-v3`.",
)

test_path = Path("tests/test_website_unicode_reconciliation.py")
if test_path.exists():
    raise SystemExit(f"unexpected existing {test_path}")
test_path.write_text(
    '''from sara.website.model import RECONCILIATION_VERSION, canonical_json\nfrom sara.website.reconcile import values_equivalent\n\n\ndef test_unicode_ascii_url_equivalence_is_versioned() -> None:\n    unicode_url = "https://مثال.إختبار/قائمة?فرع=جدة"\n    ascii_url = (\n        "https://xn--mgbh0fb.xn--kgbechtv/%D9%82%D8%A7%D8%A6%D9%85%D8%A9"\n        "?%D9%81%D8%B1%D8%B9=%D8%AC%D8%AF%D8%A9"\n    )\n    assert RECONCILIATION_VERSION == "official-web-v3"\n    assert values_equivalent(\n        "business.website.official", canonical_json(unicode_url), canonical_json(ascii_url)\n    )\n    assert not values_equivalent(\n        "business.website.official",\n        canonical_json(unicode_url),\n        canonical_json("https://other.example/"),\n    )\n''',
    encoding="utf-8",
)
