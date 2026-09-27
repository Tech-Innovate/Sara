from sara.website.model import RECONCILIATION_VERSION, canonical_json
from sara.website.reconcile import values_equivalent


def test_unicode_ascii_url_equivalence_is_versioned() -> None:
    unicode_url = "https://مثال.إختبار/قائمة?فرع=جدة"
    ascii_url = (
        "https://xn--mgbh0fb.xn--kgbechtv/%D9%82%D8%A7%D8%A6%D9%85%D8%A9"
        "?%D9%81%D8%B1%D8%B9=%D8%AC%D8%AF%D8%A9"
    )
    assert RECONCILIATION_VERSION == "official-web-v3"
    assert values_equivalent(
        "business.website.official", canonical_json(unicode_url), canonical_json(ascii_url)
    )
    assert not values_equivalent(
        "business.website.official",
        canonical_json(unicode_url),
        canonical_json("https://other.example/"),
    )
