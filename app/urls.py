"""Central URL syntax validation (no network access). See app/net.py for reachability checks.

Every URL that enters the system — typed by the user, extracted by the LLM, or returned by
web search — goes through `validate_http_url`. It never rewrites a valid URL; identity
normalization for duplicate detection lives separately in app/canonical.py.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator

MAX_URL_LENGTH = 2048
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class UrlError(ValueError):
    """The URL is not acceptable; the message is safe to show to the user."""


def validate_http_url(raw: str) -> str:
    """Return the URL (surrounding whitespace trimmed, otherwise unchanged) or raise UrlError.

    Accepts absolute http/https URLs with a valid public-style hostname or IP literal.
    Rejects: empty or overlong input, inner whitespace/control/invisible characters, backslashes,
    other schemes, missing host, embedded credentials, invalid ports and malformed hostnames.
    """
    if not isinstance(raw, str):
        raise UrlError("Enter a link as text.")
    url = raw.strip()
    if not url:
        raise UrlError("Enter a link.")
    if len(url) > MAX_URL_LENGTH:
        raise UrlError(f"The link is too long (maximum {MAX_URL_LENGTH} characters).")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url) or "\\" in url:
        raise UrlError("The link contains spaces or invalid characters.")
    if any(unicodedata.category(ch) == "Cf" for ch in url):  # zero-width and text-direction marks
        raise UrlError("The link contains invisible characters.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UrlError("The link is malformed.") from None
    if parts.scheme.lower() not in {"http", "https"}:
        raise UrlError("Only http:// and https:// links are supported.")
    if not parts.netloc or not parts.hostname:
        raise UrlError("The link is missing a website address.")
    if "@" in parts.netloc:
        raise UrlError("Links with a username or password are not allowed.")
    if port == 0:
        raise UrlError("The link has an invalid port.")
    _validate_host(parts.hostname, bracketed=parts.netloc.startswith("["))
    return url


def _validate_host(host: str, *, bracketed: bool) -> None:
    if bracketed:
        if "%" in host:
            raise UrlError("The link has an invalid IP address.")
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise UrlError("The link has an invalid IP address.") from None
        return
    try:
        ipaddress.IPv4Address(host)
        return
    except ValueError:
        pass
    name = host.rstrip(".")
    try:
        ascii_name = name.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise UrlError("The link has an invalid website address.") from None
    labels = ascii_name.split(".")
    if len(ascii_name) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise UrlError("The link has an invalid website address.")
    if labels[-1].isdigit():
        raise UrlError("The link has an invalid website address.")


HttpUrl = Annotated[str, AfterValidator(validate_http_url)]
"""Pydantic field type for request schemas: validated, original text preserved."""
