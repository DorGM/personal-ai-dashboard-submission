from __future__ import annotations

import pytest

from app import canonical as c


def test_norm_collapses_whitespace_casefolds_and_keeps_hebrew() -> None:
    assert c.norm("  The   Daily\tShow ") == "the daily show"
    assert c.norm("שלום  עולם") == "שלום עולם"
    assert c.norm(None) == ""
    assert c.norm("ﬁle") == "file"  # NFKC ligature


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://WWW.Example.com/a/b/?utm_source=x&id=2#frag", "https://example.com/a/b?id=2"),
        ("https://example.com:443/x", "https://example.com/x"),
        ("http://example.com:8080/x", "https://example.com:8080/x"),
        ("http://Example.com./jobs/1", "https://example.com/jobs/1"),
        ("https://x.com:99999/a", ""),
        ("https://x.com:abc/a", ""),
        ("http://[::1/a", ""),
        ("ftp://example.com/file", ""),
        ("javascript:alert(1)", ""),
        ("https://example.com/x?b=2&a=1&gclid=z", "https://example.com/x?a=1&b=2"),
        ("not a url", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_canonical_url(raw: str | None, expected: str) -> None:
    assert c.canonical_url(raw) == expected


def test_article_key_prefers_url_then_title_source() -> None:
    assert c.article_key("T", "S", "https://www.x.com/a/") == "url:https://x.com/a"
    assert c.article_key(" T ", "S", None) == c.article_key("t", "s", "")


def test_series_and_podcast_and_movie_keys_distinguish_missing_parts() -> None:
    assert c.series_key("Show", 1, 2) != c.series_key("Show", 1, None)
    assert c.podcast_key("Pod", None) == "pod" + c.SEP
    assert c.movie_key("Film", 1999) != c.movie_key("Film", None)


def test_job_key_precedence_matches_applications_and_recommendations() -> None:
    assert c.job_key("https://jobs.x.com/1?utm_medium=a", "E1", "A", "B", "C") == "url:https://jobs.x.com/1"
    assert c.job_key(None, " E1 ", "A", "B", "C") == "ext:e1"
    assert c.job_key(None, None, "Acme ", "Dev", "TLV") == c.job_key(None, None, "acme", "dev", "tlv")
    assert c.job_key("http://jobs.x.com/1", None, "", "", "") == c.job_key("https://jobs.x.com/1", None, "", "", "")


def test_task_key_includes_time() -> None:
    assert c.task_key("09:00", "Gym") != c.task_key(None, "Gym")


def test_separator_in_values_cannot_cause_false_duplicates() -> None:
    assert c.podcast_key("a", "b|") != c.podcast_key("a|b", None)
    assert c.job_key(None, None, "a|b", "c", "d") != c.job_key(None, None, "a", "b|c", "d")
    assert c.article_key("x|y", "z", None) != c.article_key("x", "y|z", None)
    assert c.podcast_key("a\x1fb", None) != c.podcast_key("a", "b")


def test_norm_strips_invisible_characters() -> None:
    assert c.norm("Ga\u200bme") == "game"
    assert c.norm("a\u200fb") == "ab"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://xn--bcher-kva.example/x", "https://bücher.example/x"),
        ("https://ｅｘａｍｐｌｅ.com/x", "https://example.com/x"),
        ("https://example.com。evil.com/", "https://example.com.evil.com/"),
        ("https://[2001:db8:0::1]/", "https://[2001:db8::1]/"),
    ],
)
def test_equivalent_hosts_get_the_same_key(a: str, b: str) -> None:
    assert c.canonical_url(a) == c.canonical_url(b) != ""


def test_ipv6_host_and_port_cannot_collide() -> None:
    assert c.canonical_url("https://[2001:db8::1]:8080/") != c.canonical_url("https://[2001:db8::1:8080]/")
