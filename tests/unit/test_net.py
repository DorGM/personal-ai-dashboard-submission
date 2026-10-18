from __future__ import annotations

import httpx
import pytest

from app.net import UnsafeDestination, check_destination, check_reachable, is_public_ip

PUBLIC = {"example.com": ["93.184.215.14"], "other.example.org": ["2606:4700::1111"]}


def resolver(table: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise OSError("no such host")
        return table[host]
    return resolve


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0", "100.64.0.1",
     "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1", "ff02::1", "198.18.0.1"],
)
def test_non_public_addresses_detected(address: str) -> None:
    assert not is_public_ip(address)


def test_public_addresses_allowed() -> None:
    assert is_public_ip("93.184.215.14") and is_public_ip("2606:4700::1111")


@pytest.mark.parametrize(
    ("url", "table"),
    [
        ("http://169.254.169.254/latest/meta-data", {}),
        ("https://[::1]/", {}),
        ("https://internal.example.com/", {"internal.example.com": ["10.1.2.3"]}),
        ("https://mixed.example.com/", {"mixed.example.com": ["93.184.215.14", "127.0.0.1"]}),
        ("https://nowhere.example.com/", {}),
    ],
)
def test_unsafe_destinations_refused(url: str, table: dict[str, list[str]]) -> None:
    def resolve(host: str, port: int) -> list[str]:
        if host in table:
            return table[host]
        try:
            import ipaddress
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            raise OSError("no such host") from None
    with pytest.raises(UnsafeDestination):
        check_destination(url, resolve)


def client_for(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_reachable_public_url() -> None:
    result = check_reachable("https://example.com/job", client_for(lambda r: httpx.Response(200)), resolver(PUBLIC))
    assert result.ok and result.status == 200


def test_dead_link_reported_not_raised() -> None:
    result = check_reachable("https://example.com/gone", client_for(lambda r: httpx.Response(404)), resolver(PUBLIC))
    assert not result.ok and result.status == 404


def test_redirect_to_private_address_blocked() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/admin"})

    result = check_reachable("https://example.com/r", client_for(handler), resolver(PUBLIC))
    assert not result.ok and "private or local" in result.reason
    assert seen == ["https://example.com/r"]  # the internal hop was never requested


def test_redirect_to_public_host_followed_and_validated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(301, headers={"location": "https://other.example.org/final"})
        return httpx.Response(200)

    result = check_reachable("https://example.com/r", client_for(handler), resolver(PUBLIC))
    assert result.ok and result.final_url == "https://other.example.org/final"


def test_redirect_loop_stops() -> None:
    handler = lambda r: httpx.Response(302, headers={"location": "/again"})  # noqa: E731
    result = check_reachable("https://example.com/loop", client_for(handler), resolver(PUBLIC))
    assert not result.ok and "Too many redirects" in result.reason


def test_redirect_to_bad_scheme_blocked() -> None:
    handler = lambda r: httpx.Response(302, headers={"location": "file:///etc/passwd"})  # noqa: E731
    result = check_reachable("https://example.com/r", client_for(handler), resolver(PUBLIC))
    assert not result.ok and "Only http" in result.reason


def test_network_error_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    result = check_reachable("https://example.com/x", client_for(handler), resolver(PUBLIC))
    assert not result.ok and "ConnectTimeout" in result.reason


@pytest.mark.parametrize("address", ["64:ff9b::7f00:1", "64:ff9b:1::a00:1", "::127.0.0.1", "::a00:1",
                                     "2002:7f00:1::", "2001:0:4136:e378:8000:63bf:3fff:fdd2"])
def test_translated_ipv6_addresses_are_not_public(address: str) -> None:
    assert not is_public_ip(address)


@pytest.mark.parametrize("location", ["javascript:alert(1)", "http://[::1", "http://exa mple.com/", "//10.0.0.1/"])
def test_bad_redirect_targets_reported_not_raised(location: str) -> None:
    client = client_for(lambda r: httpx.Response(302, headers={"location": location}))
    result = check_reachable("https://example.com/a", client, resolver(PUBLIC))
    assert not result.ok


def test_resolver_unicode_error_is_reported_as_unsafe() -> None:
    def resolve(host: str, port: int) -> list[str]:
        raise UnicodeError("label too long")
    with pytest.raises(UnsafeDestination):
        check_destination("https://example.com/", resolve)
