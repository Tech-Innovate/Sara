from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_HIGH_VALUE_TERMS = {
    "about": 80,
    "service": 100,
    "services": 100,
    "product": 95,
    "products": 95,
    "menu": 100,
    "pricing": 100,
    "price": 90,
    "location": 95,
    "locations": 95,
    "contact": 100,
    "booking": 110,
    "book": 105,
    "reservation": 110,
    "reserve": 105,
    "order": 110,
    "ordering": 110,
    "faq": 80,
    "careers": 70,
    "career": 70,
    "support": 85,
    "help": 75,
}
_BOOKING_TERMS = {
    "booking",
    "reserve",
    "reservation",
    "appointment",
    "appointments",
    "schedule",
    "scheduling",
}
_BOOKING_ACTION_TERMS = {
    "now",
    "online",
    "appointment",
    "appointments",
    "table",
    "tables",
    "visit",
    "session",
    "sessions",
    "consultation",
    "consultations",
    "slot",
    "slots",
    "today",
}
_ORDERING_TERMS = {"order", "ordering", "delivery", "deliver", "pickup", "takeaway", "takeout"}
_SUPPORT_TERMS = {"support", "help", "helpdesk"}
_SOCIAL_HOSTS = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "linkedin.com": "linkedin",
    "x.com": "x",
    "twitter.com": "x",
    "tiktok.com": "tiktok",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
}
_BOOKING_DOMAINS = {
    "booksy.com",
    "calendly.com",
    "opentable.com",
    "resy.com",
    "fresha.com",
    "mindbodyonline.com",
}
_ORDERING_DOMAINS = {
    "doordash.com",
    "ubereats.com",
    "talabat.com",
    "hungerstation.com",
    "jahez.net",
    "deliveroo.com",
}


@dataclass(frozen=True)
class LinkCandidate:
    url: str
    text: str
    rel: tuple[str, ...]
    priority: int


@dataclass(frozen=True)
class ChannelCandidate:
    channel_type: str
    identifier: str
    normalized_identifier: str
    url: str | None
    extraction: str


@dataclass(frozen=True)
class ParsedPage:
    title: str | None
    canonical_url: str | None
    links: tuple[LinkCandidate, ...]
    channels: tuple[ChannelCandidate, ...]
    booking_detected: bool
    ordering_detected: bool
    whatsapp_detected: bool


def _host(value: str) -> str:
    try:
        host = (urlsplit(value).hostname or "").lower().rstrip(".")
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
        and not any(
            key.lower().startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES
        )
    ]
    query = urlencode(pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}


def _priority(url: str, text: str) -> int:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return 0
    tokens = _tokens(parsed.path + " " + text)
    score = 0
    for token in tokens:
        score = max(score, _HIGH_VALUE_TERMS.get(token, 0))
    depth_penalty = max(
        0, len([part for part in parsed.path.split("/") if part]) - 1
    ) * 3
    return score - depth_penalty


def _normalize_phone(value: str) -> str | None:
    cleaned = re.sub(r"[^0-9+]", "", value.strip())
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not cleaned or cleaned == "+":
        return None
    return cleaned


def _domain_matches(host: str, domains: set[str]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(normalized == domain or normalized.endswith("." + domain) for domain in domains)


def _social_type(host: str) -> str | None:
    normalized = host.lower().rstrip(".")
    for suffix, channel_type in _SOCIAL_HOSTS.items():
        if normalized == suffix or normalized.endswith("." + suffix):
            return channel_type
    return None


def _is_social_profile(url: str, channel_type: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    parts = [part.lower() for part in parsed.path.split("/") if part]
    if not parts:
        return False
    first = parts[0]
    if channel_type == "instagram":
        return first not in {
            "accounts",
            "explore",
            "p",
            "reel",
            "reels",
            "stories",
            "share",
        }
    if channel_type == "facebook":
        return first not in {
            "dialog",
            "plugins",
            "share",
            "sharer",
            "watch",
            "reel",
            "story.php",
        }
    if channel_type == "linkedin":
        return first in {"company", "school", "showcase"} and len(parts) >= 2
    if channel_type == "x":
        return first not in {
            "home",
            "i",
            "intent",
            "search",
            "settings",
            "share",
        }
    if channel_type == "tiktok":
        return first.startswith("@") and len(first) > 1
    if channel_type == "youtube":
        return first.startswith("@") or (
            first in {"channel", "c", "user"} and len(parts) >= 2
        )
    return False


def classify_channel(
    href: str, text: str, page_url: str
) -> ChannelCandidate | None:
    raw = html.unescape(href).strip()
    lower = raw.lower()
    if lower.startswith("mailto:"):
        address = raw[7:].split("?", 1)[0].strip().lower()
        if not address or "@" not in address:
            return None
        return ChannelCandidate(
            "email", address, address, f"mailto:{address}", "direct_link"
        )
    if lower.startswith("tel:"):
        original = raw[4:].split("?", 1)[0].strip()
        normalized = _normalize_phone(original)
        if normalized is None:
            return None
        return ChannelCandidate(
            "phone", normalized, normalized, f"tel:{normalized}", "direct_link"
        )

    url = normalize_http_url(raw, page_url)
    if url is None:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    path_tokens = _tokens(parsed.path)
    text_tokens = _tokens(text)
    combined = path_tokens | text_tokens

    if host == "wa.me" or host.endswith(".whatsapp.com") or host == "whatsapp.com":
        return ChannelCandidate(
            "whatsapp", url, url, url, "direct_link"
        )

    social = _social_type(host)
    if social and _is_social_profile(url, social):
        return ChannelCandidate(social, url, url, url, "direct_link")

    explicit_booking_text = bool(text_tokens & _BOOKING_TERMS) or (
        "book" in text_tokens and bool(text_tokens & _BOOKING_ACTION_TERMS)
    )
    if _domain_matches(host, _BOOKING_DOMAINS) or (
        bool(combined & (_BOOKING_TERMS | {"book"})) and explicit_booking_text
    ):
        return ChannelCandidate("booking", url, url, url, "action_link")
    if _domain_matches(host, _ORDERING_DOMAINS) or (
        bool(combined & _ORDERING_TERMS) and bool(text_tokens & _ORDERING_TERMS)
    ):
        return ChannelCandidate("ordering", url, url, url, "action_link")
    if bool(combined & _SUPPORT_TERMS) and bool(text_tokens & _SUPPORT_TERMS):
        return ChannelCandidate("support", url, url, url, "action_link")
    return None


class _PageParser(HTMLParser):
    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self._in_title = False
        self._title_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_rel: tuple[str, ...] = ()
        self._anchor_text: list[str] = []
        self._links: list[tuple[str, str, tuple[str, ...]]] = []
        self.canonical_url: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        name = tag.lower()
        if name == "title":
            self._in_title = True
        elif name == "link":
            rel = {token.lower() for token in values.get("rel", "").split()}
            if "canonical" in rel and values.get("href"):
                candidate = normalize_http_url(values["href"], self.page_url)
                if candidate is not None:
                    self.canonical_url = candidate
        elif name == "a":
            self._anchor_href = values.get("href") or None
            self._anchor_rel = tuple(
                sorted(token.lower() for token in values.get("rel", "").split())
            )
            self._anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name == "title":
            self._in_title = False
        elif name == "a" and self._anchor_href is not None:
            text = " ".join("".join(self._anchor_text).split())
            self._links.append((self._anchor_href, text, self._anchor_rel))
            self._anchor_href = None
            self._anchor_rel = ()
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._anchor_href is not None:
            self._anchor_text.append(data)

    def result(self) -> ParsedPage:
        title = " ".join("".join(self._title_parts).split()) or None
        link_map: dict[str, LinkCandidate] = {}
        channel_map: dict[tuple[str, str], ChannelCandidate] = {}
        booking = ordering = whatsapp = False
        for href, text, rel in self._links:
            channel = classify_channel(href, text, self.page_url)
            if channel is not None:
                channel_map[(channel.channel_type, channel.normalized_identifier)] = channel
                booking = booking or channel.channel_type == "booking"
                ordering = ordering or channel.channel_type == "ordering"
                whatsapp = whatsapp or channel.channel_type == "whatsapp"

            url = normalize_http_url(href, self.page_url)
            if url is None or "nofollow" in rel:
                continue
            candidate = LinkCandidate(
                url=url, text=text, rel=rel, priority=_priority(url, text)
            )
            previous = link_map.get(url)
            if previous is None or candidate.priority > previous.priority:
                link_map[url] = candidate

        links = tuple(
            sorted(link_map.values(), key=lambda item: (-item.priority, item.url))
        )
        channels = tuple(
            sorted(
                channel_map.values(),
                key=lambda item: (item.channel_type, item.normalized_identifier),
            )
        )
        return ParsedPage(
            title=title,
            canonical_url=self.canonical_url,
            links=links,
            channels=channels,
            booking_detected=booking,
            ordering_detected=ordering,
            whatsapp_detected=whatsapp,
        )


def parse_html(page_url: str, text: str) -> ParsedPage:
    parser = _PageParser(page_url)
    parser.feed(text)
    parser.close()
    return parser.result()
