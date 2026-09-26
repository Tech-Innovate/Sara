from sara.website.parser import normalize_http_url, parse_html, same_site


def test_normalize_http_url_strips_tracking_and_fragment() -> None:
    assert (
        normalize_http_url(
            "/services?utm_source=maps&keep=1&fbclid=abc#section",
            "https://Example.COM/root",
        )
        == "https://example.com/services?keep=1"
    )


def test_same_site_is_conservative_about_subdomains() -> None:
    assert same_site("https://www.example.com/a", "https://example.com/")
    assert same_site("https://example.com/a", "https://www.example.com/")
    assert not same_site("https://booking.example.com/a", "https://example.com/")
    assert not same_site("https://example.net/a", "https://example.com/")


def test_parser_extracts_high_value_links_and_channels() -> None:
    page = parse_html(
        "https://example.com/",
        """
        <html><head>
          <title> Example Business </title>
          <link rel="canonical" href="https://example.com/">
        </head><body>
          <a href="/about">About us</a>
          <a href="/services">Services</a>
          <a href="/book">Book now</a>
          <a href="mailto:HELLO@EXAMPLE.COM">Email</a>
          <a href="tel:+966 50 123 4567">Call</a>
          <a href="https://wa.me/966501234567">WhatsApp</a>
          <a href="https://instagram.com/example">Instagram</a>
          <a rel="nofollow" href="/ignored">Ignored</a>
        </body></html>
        """,
    )
    assert page.title == "Example Business"
    assert page.canonical_url == "https://example.com/"
    assert page.booking_detected is True
    assert page.whatsapp_detected is True
    assert page.ordering_detected is False
    assert page.links[0].url == "https://example.com/book"
    assert all(link.url != "https://example.com/ignored" for link in page.links)
    channels = {(item.channel_type, item.normalized_identifier) for item in page.channels}
    assert ("email", "hello@example.com") in channels
    assert ("phone", "+966501234567") in channels
    assert ("instagram", "https://instagram.com/example") in channels
    assert any(channel_type == "whatsapp" for channel_type, _identifier in channels)


def test_booking_heuristic_requires_action_context_for_same_site_links() -> None:
    neutral = parse_html(
        "https://example.com/",
        '<a href="/book-history">Our book history</a>',
    )
    assert neutral.booking_detected is False

    action = parse_html(
        "https://example.com/",
        '<a href="/appointments">Book appointment</a>',
    )
    assert action.booking_detected is True
