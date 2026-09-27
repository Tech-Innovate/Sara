from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"expected one match in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


old = '''def _ascii_host(value: str) -> str | None:
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
'''
new = '''def _valid_ascii_hostname(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    labels = host.split(".")
    return all(
        label
        and len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and re.fullmatch(r"[a-z0-9-]+", label) is not None
        for label in labels
    )


def _ascii_host(value: str) -> str | None:
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

    if host.isascii():
        return host if _valid_ascii_hostname(host) else None

    try:
        ascii_host = host.encode("idna").decode("ascii").lower().rstrip(".")
        round_trip = ascii_host.encode("ascii").decode("idna").lower().rstrip(".")
    except UnicodeError:
        return None
    # The stdlib codec implements legacy IDNA mappings (for example ß -> ss).
    # Do not silently contact a different ASCII hostname when the mapping is not
    # reversible. A future IDNA2008/UTS-46 dependency can broaden this safely.
    if round_trip != host or not _valid_ascii_hostname(ascii_host):
        return None
    return ascii_host
'''
replace_once("src/sara/website/parser.py", old, new)

path = Path("tests/test_website_parser.py")
text = path.read_text(encoding="utf-8")
marker = "def test_ambiguous_or_invalid_idna_host_fails_closed"
if marker not in text:
    text += '''\n\ndef test_ambiguous_or_invalid_idna_host_fails_closed() -> None:
    # The stdlib IDNA codec maps these non-reversibly or leaves invalid ASCII
    # hostname characters untouched. Sara must not silently change destination.
    for raw in (
        "https://faß.de/",
        "https://e\\u0301xample.com/",
        "https://exa_mple.com/",
        "https://example com/",
    ):
        assert normalize_http_url(raw) is None
\n\ndef test_reversible_idna_hosts_remain_supported() -> None:
    assert normalize_http_url("https://例え.テスト/道") == (
        "https://xn--r8jz45g.xn--zckzah/%E9%81%93"
    )
'''
    path.write_text(text, encoding="utf-8")
