from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"expected one match in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


old = '''def _valid_ascii_hostname(host: str) -> bool:
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

new = '''def _valid_ascii_label(label: str) -> bool:
    return bool(
        label
        and len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and re.fullmatch(r"[a-z0-9-]+", label) is not None
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

    ascii_labels: list[str] = []
    for label in host.split("."):
        if not label:
            return None
        if label.isascii():
            if not _valid_ascii_label(label):
                return None
            # Retain already-ASCII labels verbatim, including valid xn-- labels.
            # Decoding them would change mixed Unicode/punycode host identity.
            ascii_labels.append(label)
            continue
        try:
            ascii_label = label.encode("idna").decode("ascii").lower()
            round_trip = ascii_label.encode("ascii").decode("idna").lower()
        except UnicodeError:
            return None
        # The stdlib codec implements legacy IDNA mappings (for example ß -> ss).
        # Require reversibility for each Unicode label independently so mixed
        # Unicode/punycode hostnames remain valid without destination rewriting.
        if round_trip != label or not _valid_ascii_label(ascii_label):
            return None
        ascii_labels.append(ascii_label)

    ascii_host = ".".join(ascii_labels)
    return ascii_host if len(ascii_host) <= 253 else None
'''
replace_once("src/sara/website/parser.py", old, new)

path = Path("tests/test_website_parser.py")
text = path.read_text(encoding="utf-8")
marker = "def test_mixed_punycode_and_unicode_labels_are_supported"
if marker not in text:
    text += '''\n\ndef test_mixed_punycode_and_unicode_labels_are_supported() -> None:\n    assert normalize_http_url("https://xn--r8jz45g.テスト/道") == (\n        "https://xn--r8jz45g.xn--zckzah/%E9%81%93"\n    )\n    assert same_site(\n        "https://xn--r8jz45g.テスト/道",\n        "https://xn--r8jz45g.xn--zckzah/",\n    )\n'''
    path.write_text(text, encoding="utf-8")
