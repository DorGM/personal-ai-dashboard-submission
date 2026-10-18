"""Deterministic identity keys used by UNIQUE constraints (A-03 §3)."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.urls import UrlError, validate_http_url

_WS = re.compile(r"\s+")
# Unit separator: cannot appear in normalized text (control chars are stripped), so joined
# fields can never collide the way "a|b" + "c" and "a" + "b|c" would.
SEP = "\x1f"
_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "trk", "trackingid"}


def norm(text: str | None) -> str:
    """NFKC, drop invisible format/control chars, collapse whitespace, casefold. Hebrew kept as-is."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(" " if ch.isspace() else ch for ch in text if unicodedata.category(ch) not in {"Cf", "Cc"}
                   or ch.isspace())
    return _WS.sub(" ", text).strip().casefold()


def canonical_url(url: str | None) -> str:
    """Identity form of an http(s) URL; "" if missing or invalid (see app.urls).

    Used only for duplicate detection, never for display or storage of the link itself:
    http and https are treated as the same resource; the host is converted to its ASCII
    (IDNA/punycode) form, lowercased, without "www." or a trailing dot, and IPv6 addresses
    keep their brackets in compressed form; default ports, fragment, tracking params and trailing slash are dropped.
    """
    if not url:
        return ""
    try:
        valid = validate_http_url(url)
    except UrlError:
        return ""
    parts = urlsplit(valid)
    host = _canonical_host(parts.hostname or "", bracketed=parts.netloc.startswith("["))
    if parts.port and parts.port not in {80, 443}:
        host = f"{host}:{parts.port}"
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
        )
    )
    path = parts.path.rstrip("/")
    return urlunsplit(("https", host, path, query, ""))


def _canonical_host(hostname: str, *, bracketed: bool) -> str:
    if bracketed:
        return f"[{ipaddress.IPv6Address(hostname).compressed}]"
    try:
        return str(ipaddress.IPv4Address(hostname))
    except ValueError:
        pass
    # The idna codec applies NFKC (fullwidth letters, ideographic dots) as browsers do.
    return hostname.rstrip(".").encode("idna").decode("ascii").lower().rstrip(".").removeprefix("www.")


def _opt(value: object) -> str:
    return "" if value is None else str(value)


def _join(*parts: str) -> str:
    return SEP.join(parts)


def article_key(title: str, source: str | None, url: str | None) -> str:
    cu = canonical_url(url)
    return f"url:{cu}" if cu else "t:" + _join(norm(title), norm(source))


def podcast_key(podcast_name: str, episode_title: str | None) -> str:
    return _join(norm(podcast_name), norm(episode_title))


def movie_key(title: str, release_year: int | None) -> str:
    return _join(norm(title), _opt(release_year))


def series_key(series_name: str, season: int | None, episode: int | None) -> str:
    return _join(norm(series_name), _opt(season), _opt(episode))


def job_key(
    url: str | None, external_id: str | None, company: str, title: str, location: str
) -> str:
    """Shared by job_applications and job_recommendations so they can be matched."""
    cu = canonical_url(url)
    if cu:
        return f"url:{cu}"
    if external_id and external_id.strip():
        return f"ext:{norm(external_id)}"
    return "cr:" + _join(norm(company), norm(title), norm(location))


def trend_key(name: str) -> str:
    return norm(name)


def task_key(start_time: str | None, title: str) -> str:
    return _join(_opt(start_time), norm(title))
