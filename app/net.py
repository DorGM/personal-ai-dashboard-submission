"""Destination checks for URLs the backend fetches (SSRF protection).

Syntax validation (app/urls.py) is separate from availability: `check_reachable` is only for
links the server itself requests (e.g. verifying web-search results in Run now). Every hop,
including each redirect, is validated and resolved; any non-public address is refused.

Residual risk: a hostname could resolve differently between our check and httpx's own
connection (DNS rebinding). Mitigated by short timeouts, no credentials sent, small redirect
limit and response bodies never being read.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from app.urls import UrlError, validate_http_url

Resolver = Callable[[str, int], list[str]]
MAX_REDIRECTS = 3
# IPv6 ranges that embed or translate to an IPv4 address (NAT64, IPv4-compatible, 6to4,
# Teredo): refused outright rather than trusting the embedded address.
_TRANSLATED_V6 = tuple(
    ipaddress.IPv6Network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "::/96", "2002::/16", "2001::/32")
)


class UnsafeDestination(ValueError):
    """The URL points at a private, local or otherwise non-public address."""


@dataclass(frozen=True)
class Reachability:
    ok: bool
    status: int | None
    final_url: str
    reason: str


def system_resolver(host: str, port: int) -> list[str]:
    return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _TRANSLATED_V6):
            return False
    return ip.is_global and not ip.is_multicast


def check_destination(url: str, resolve: Resolver = system_resolver) -> None:
    """Raise UrlError for bad syntax, UnsafeDestination if any resolved address is not public."""
    validate_http_url(url)
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        addresses = [str(ipaddress.ip_address(host))]  # IP literal: nothing to resolve
    except ValueError:
        addresses = None
    try:
        addresses = addresses or resolve(host, port)
    except (OSError, UnicodeError, ValueError):
        raise UnsafeDestination("The website address could not be resolved.") from None
    try:
        public = bool(addresses) and all(is_public_ip(a) for a in addresses)
    except ValueError:
        public = False
    if not public:
        raise UnsafeDestination("The link points to a private or local network address.")


def check_reachable(url: str, client: httpx.Client, resolve: Resolver = system_resolver) -> Reachability:
    """Request the URL without following redirects automatically; validate every hop.

    Never raises for a bad link, redirect or network problem: the outcome is in the result.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            check_destination(current, resolve)
        except (UrlError, UnsafeDestination) as exc:
            return Reachability(False, None, current, str(exc))
        try:
            with client.stream("GET", current, follow_redirects=False) as response:
                status = response.status_code
                location = response.headers.get("location")
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, UnicodeError) as exc:
            return Reachability(False, None, current, f"Request failed: {type(exc).__name__}")
        if 300 <= status < 400 and location:
            current = urljoin(current, location)
            continue
        return Reachability(200 <= status < 300, status, current, "ok" if status < 300 else f"HTTP {status}")
    return Reachability(False, None, current, "Too many redirects.")


def make_client() -> httpx.Client:
    """Client for reachability checks: short timeouts, no cookies kept between hosts."""
    return httpx.Client(timeout=httpx.Timeout(5.0), headers={"User-Agent": "personal-ai-dashboard/0.1"})
