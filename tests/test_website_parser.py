from sara.website.parser import normalize_http_url, parse_html, same_site


def test_normalize_http_url_strips_tracking_and_fragment() -> None:
    assert (
        normalize_http_url(
            "/services?utm_source=maps&keep=1&fbclid=abc#section",
            "https://Example.COM/root",
        )
        == "https://example.com/services?keep=1"
    )


def test_normalize_http_url_rejects_malformed_ports_and_keeps_ipv6_brackets() -> None:
    assert normalize_http_url("https://example.com:notaport/path") is None
    assert normalize_http_url("https://[2001:4860:4860::8888]/path") == (
        "https://[2001:4860:4860::8888]/path"
    )


def test_same_site_is_conservative_about_subdomains_and_malformed_urls() -> None:
    assert same_site("https://www.example.com/a", "https://example.com/")
    assert same_site("https://example.com/a", "https://www.example.com/")
    assert not same_site("https://booking.example.com/a", "https://example.com/")
    assert not same_site("https://example.net/a", "https://example.com/")
    assert not same_site("https://example.com:bad/a", "https://example.com/")


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


def test_booking_heuristic_requires_transactional_action_context() -> None:
    for label, href in (
        ("Our book history", "/book-history"),
        ("Booking policy", "/booking-policy"),
        ("Appointment information", "/appointments/info"),
    ):
        page = parse_html("https://example.com/", f'<a href="{href}">{label}</a>')
        assert page.booking_detected is False

    for label, href in (
        ("Book appointment", "/appointments"),
        ("Book now", "/book"),
        ("Reservations", "/reservations"),
        ("Schedule consultation", "/consultation"),
    ):
        page = parse_html("https://example.com/", f'<a href="{href}">{label}</a>')
        assert page.booking_detected is True


def test_ordering_heuristic_requires_transactional_action_context() -> None:
    for label, href in (
        ("Order history", "/order-history"),
        ("Delivery policy", "/delivery-policy"),
        ("Pickup information", "/pickup-info"),
    ):
        page = parse_html("https://example.com/", f'<a href="{href}">{label}</a>')
        assert page.ordering_detected is False

    for label, href in (
        ("Order", "/order"),
        ("Order online", "/order"),
        ("Order delivery", "/delivery"),
        ("Online ordering", "/ordering"),
    ):
        page = parse_html("https://example.com/", f'<a href="{href}">{label}</a>')
        assert page.ordering_detected is True


def test_malformed_page_link_is_ignored_without_losing_other_page_signals() -> None:
    page = parse_html(
        "https://example.com/",
        """
        <a href="https://example.com:notaport/broken">Broken</a>
        <a href="mailto:hello@example.com">Email</a>
        <a href="/services">Services</a>
        """,
    )
    assert [link.url for link in page.links] == ["https://example.com/services"]
    assert any(item.channel_type == "email" for item in page.channels)


def test_social_share_and_content_links_are_not_promoted_as_profiles() -> None:
    page = parse_html(
        "https://example.com/",
        """
        <a href="https://facebook.com/sharer/sharer.php?u=https://example.com">Share</a>
        <a href="https://instagram.com/p/abc123">Post</a>
        <a href="https://linkedin.com/in/person">Person</a>
        <a href="https://youtube.com/watch?v=abc">Video</a>
        <a href="https://instagram.com/example">Instagram profile</a>
        <a href="https://linkedin.com/company/example">LinkedIn company</a>
        <a href="https://youtube.com/@example">YouTube channel</a>
        """,
    )
    channels = {(item.channel_type, item.normalized_identifier) for item in page.channels}
    assert ("instagram", "https://instagram.com/example") in channels
    assert ("linkedin", "https://linkedin.com/company/example") in channels
    assert ("youtube", "https://youtube.com/@example") in channels
    assert all("sharer" not in identifier for _kind, identifier in channels)
    assert all("/p/" not in identifier for _kind, identifier in channels)
    assert all("/in/" not in identifier for _kind, identifier in channels)
    assert all("/watch" not in identifier for _kind, identifier in channels)


def test_platform_host_hints_require_real_domain_boundary() -> None:
    false_booking = parse_html(
        "https://example.com/",
        '<a href="https://notcalendly.com/path">Visit partner</a>',
    )
    assert false_booking.booking_detected is False

    true_booking = parse_html(
        "https://example.com/",
        '<a href="https://team.calendly.com/example">Schedule</a>',
    )
    assert true_booking.booking_detected is True
