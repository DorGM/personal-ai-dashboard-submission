from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.urls import HttpUrl, UrlError, validate_http_url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com/path?q=1#frag",
        "https://sub.example.co.il/a/b/",
        "https://example.com:8443/x",
        "https://xn--4dbrk0ce.co.il/",
        "https://שלום.co.il/page",
        "http://203.0.113.5/report",
        "https://[2001:db8::1]/x",
        "HTTPS://Example.COM/Mixed/Case",
        "https://example.com./trailing-dot",
        "https://my-site.example.org/%D7%A9",
    ],
)
def test_valid_urls_are_accepted_unchanged(url: str) -> None:
    assert validate_http_url(url) == url


def test_surrounding_whitespace_is_trimmed_only() -> None:
    assert validate_http_url("  https://example.com/A?b=C  ") == "https://example.com/A?b=C"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("", "Enter a link"),
        ("   ", "Enter a link"),
        ("example.com", "Only http"),
        ("ftp://example.com/file", "Only http"),
        ("javascript:alert(1)", "Only http"),
        ("file:///etc/passwd", "Only http"),
        ("data:text/html,hi", "Only http"),
        ("https://", "missing a website"),
        ("https:///path", "missing a website"),
        ("//example.com/x", "Only http"),
        ("https://user:pass@example.com/", "username or password"),
        ("https://user@example.com/", "username or password"),
        ("https://exa mple.com/a", "spaces"),
        ("https://example.com/a b", "spaces"),
        ("https://example.com/\x00", "spaces"),
        ("https://example.com\\@evil.com", "invalid characters"),
        ("https://example.com:99999/", "malformed"),
        ("https://example.com:abc/", "malformed"),
        ("https://example.com:0/", "invalid port"),
        ("http://[::1/a", "malformed"),
        ("https://[not-ipv6]/", "malformed|invalid IP"),
        ("https://[fe80::1%25eth0]/", "malformed|invalid IP"),
        ("https://-bad-.com/", "invalid website"),
        ("https://bad_host.com/", "invalid website"),
        ("https://localhost/", "invalid website"),
        ("https://127.1/", "invalid website"),
        ("https://0x7f.1/", "invalid website"),
        ("https://" + "a" * 64 + ".com/", "invalid website"),
        ("https://example.com/" + "a" * 2100, "too long"),
    ],
)
def test_invalid_urls_rejected_with_clear_message(url: str, message: str) -> None:
    with pytest.raises(UrlError, match=message):
        validate_http_url(url)


def test_non_string_rejected() -> None:
    with pytest.raises(UrlError):
        validate_http_url(123)  # type: ignore[arg-type]


def test_pydantic_field_type_uses_same_rules() -> None:
    class Form(BaseModel):
        url: HttpUrl

    assert Form(url=" https://example.com/x ").url == "https://example.com/x"
    with pytest.raises(ValidationError, match="username or password"):
        Form(url="https://u:p@example.com")


@pytest.mark.parametrize("url", ["https://ex​ample.com/", "https://example.com/‮abc", "https://example.com/﻿"])
def test_invisible_characters_rejected(url: str) -> None:
    with pytest.raises(UrlError, match="invisible"):
        validate_http_url(url)
