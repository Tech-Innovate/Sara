from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"expected exactly one match in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/sara/website/parser.py",
    'from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit\n',
    'from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit\n',
)

old_block = '''def _host(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, site_url: str) -> bool:
    candidate = _host(url)
    site = _host(site_url)
    return bool(candidate and site and candidate == site)


def normalize_http_url(value: str, base_url: str | None = None) -> str | None:
    value = html.unescape(value).strip()
    if not value:
        return None
    try:
        absolute = urljoin(base_url, value) if base_url else value
        parsed = urlsplit(absolute)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not host:
        return None
    display_host = f"[{host}]" if ":" in host else host
    if port and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        netloc = f"{display_host}:{port}"
    else:
        netloc = display_host
    path = parsed.path or "/"
    pairs = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS
        and not any(key.lower().startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES)
    ]
    query = urlencode(pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))
'''

new_block = '''def _ascii_host(value: str) -> str | None:
    host = value.lower().rstrip(".")
    if not host:
        return None
    if ":" in host:
        # Bracketed IPv6 literals are returned by ``urlsplit().hostname``
        # without brackets. They must already be ASCII; address validity and
        # public/private classification remain the HTTP client's responsibility.
        try:
            host.encode("ascii")
        except UnicodeError:
            return None
        return host
    try:
        ascii_host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return None
    if not ascii_host:
        return None
    return ascii_host


def _host(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        host = _ascii_host(parsed.hostname or "")
    except (UnicodeError, ValueError):
        return ""
    if host is None:
        return ""
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, site_url: str) -> bool:
    candidate = _host(url)
    site = _host(site_url)
    return bool(candidate and site and candidate == site)


def normalize_http_url(value: str, base_url: str | None = None) -> str | None:
    value = html.unescape(value).strip()
    if not value:
        return None
    try:
        absolute = urljoin(base_url, value) if base_url else value
        parsed = urlsplit(absolute)
        scheme = parsed.scheme.lower()
        host = _ascii_host(parsed.hostname or "")
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if scheme not in {"http", "https"} or not host:
        return None
    display_host = f"[{host}]" if ":" in host else host
    if port and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        netloc = f"{display_host}:{port}"
    else:
        netloc = display_host
    try:
        # HTTP request targets are byte-oriented. Keep RFC 3986 path delimiters
        # and existing percent escapes, while UTF-8 percent-encoding raw Unicode
        # before the value can reach ``http.client``'s ASCII serialization.
        path = quote(
            parsed.path or "/",
            safe="/!$&'()*+,;=:@-._~%",
            encoding="utf-8",
            errors="strict",
        )
        pairs = [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_QUERY_KEYS
            and not any(key.lower().startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES)
        ]
        query = urlencode(pairs, doseq=True, encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError):
        return None
    normalized = urlunsplit((scheme, netloc, path, query, ""))
    try:
        normalized.encode("ascii")
    except UnicodeError:
        return None
    return normalized
'''
replace_once("src/sara/website/parser.py", old_block, new_block)

replace_once(
    "src/sara/website/model.py",
    'COLLECTOR_VERSION = "3"\n',
    'COLLECTOR_VERSION = "4"\n',
)

parser_tests = Path("tests/test_website_parser.py")
text = parser_tests.read_text(encoding="utf-8")
marker = "def test_unicode_transport_urls_are_canonical_ascii"
if marker not in text:
    text += '''\n\ndef test_unicode_transport_urls_are_canonical_ascii() -> None:
    # Production businesses 141 and 248 exposed this shape: an ASCII host with
    # raw non-ASCII URL characters. The exact retained production URLs are not
    # fixtures in this repository, so use representative Arabic path/query data
    # on the observed hosts.
    for raw in (
        "https://mandi-hdoon.com/منيو/المندي?branch=جدة",
        "https://stovejeddah.com/مطعم/جدة?menu=العشاء",
    ):
        normalized = normalize_http_url(raw)
        assert normalized is not None
        normalized.encode("ascii")
        assert "%D8%" in normalized


def test_unicode_hostname_is_idna_encoded_before_transport() -> None:
    normalized = normalize_http_url("https://مثال.إختبار/قائمة?فرع=جدة")
    assert normalized == (
        "https://xn--mgbh0fb.xn--kgbechtv/%D9%82%D8%A7%D8%A6%D9%85%D8%A9"
        "?%D9%81%D8%B1%D8%B9=%D8%AC%D8%AF%D8%A9"
    )
    assert same_site(
        "https://مثال.إختبار/قائمة",
        "https://xn--mgbh0fb.xn--kgbechtv/",
    )


def test_existing_percent_escapes_are_not_double_encoded() -> None:
    assert normalize_http_url("https://example.com/%D9%82%D8%A7%D8%A6%D9%85%D8%A9") == (
        "https://example.com/%D9%82%D8%A7%D8%A6%D9%85%D8%A9"
    )


def test_malformed_unicode_url_fails_closed() -> None:
    assert normalize_http_url("https://example.com/\\ud800") is None
    assert normalize_http_url("https://\\ud800.example/path") is None
'''
    parser_tests.write_text(text, encoding="utf-8")

http_tests = Path("tests/test_website_http.py")
text = http_tests.read_text(encoding="utf-8")
marker = "def test_unicode_site_uses_ascii_idna_for_dns_and_security"
if marker not in text:
    text += '''\n\ndef test_unicode_site_uses_ascii_idna_for_dns_and_security() -> None:
    lookups: list[tuple[str, int]] = []

    def lookup(host, port, **_kwargs):
        lookups.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    client = SafeHttpClient(
        site_url="https://مثال.إختبار/قائمة",
        user_agent="SaraBusinessUnderstanding/1.0",
        timeout_seconds=2,
        max_response_bytes=65536,
        dns_lookup=lookup,
    )
    assert client.site_url == "https://xn--mgbh0fb.xn--kgbechtv/%D9%82%D8%A7%D8%A6%D9%85%D8%A9"
    assert client._resolve_public_addresses(client.site_url) == ("93.184.216.34",)
    assert lookups == [("xn--mgbh0fb.xn--kgbechtv", 443)]
    client._assert_allowed_site("https://xn--mgbh0fb.xn--kgbechtv/%D9%81%D8%B1%D8%B9")


def test_unicode_site_still_runs_ssrf_guard_after_idna_normalization() -> None:
    def lookup(_host, port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    client = SafeHttpClient(
        site_url="https://مثال.إختبار/قائمة",
        user_agent="SaraBusinessUnderstanding/1.0",
        timeout_seconds=2,
        max_response_bytes=65536,
        dns_lookup=lookup,
    )
    with pytest.raises(WebsiteBlockedError, match="non-public"):
        client._assert_public_host(client.site_url)


def test_unicode_request_target_is_ascii_serializable() -> None:
    client = _client("93.184.216.34")
    normalized = client.site_url.replace("example.com/", "example.com/%D9%82%D8%A7%D8%A6%D9%85%D8%A9")
    target = client._request_target(__import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(normalized))
    assert target == "/%D9%82%D8%A7%D8%A6%D9%85%D8%A9"
    target.encode("ascii")
'''
    http_tests.write_text(text, encoding="utf-8")

runbook = Path("docs/website-operational-validation.md")
text = runbook.read_text(encoding="utf-8")
heading = "## Unicode/IDNA transport hardening"
if heading not in text:
    text += '''\n\n## Unicode/IDNA transport hardening\n\nOfficial-site URLs are canonicalized to an ASCII network representation before DNS, TLS, robots, SSRF checks, origin comparison, and HTTP request serialization. Unicode hostname labels are IDNA-encoded; raw Unicode path/query characters are UTF-8 percent-encoded. Invalid Unicode/IDNA fails closed. This transport-normalization change is versioned with website collector version `4`; website reconciliation remains `official-web-v2`.\n'''
    runbook.write_text(text, encoding="utf-8")
