"""Tests for the SSRF guard.

All cases are deterministic: hosts are IP literals (no external DNS) and the
guard policy is injected per test.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from tai42_kit.net import url_guard
from tai42_kit.net.url_guard import UrlGuardError, UrlGuardSettings


def _enable(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> UrlGuardSettings:
    settings = UrlGuardSettings(enabled=True, **kwargs)
    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: settings)
    return settings


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: UrlGuardSettings(enabled=False))


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.1",
        "192.168.1.1",
        "::1",
    ],
)
async def test_resolve_and_validate_blocks_non_public_hosts(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    _enable(monkeypatch)
    with pytest.raises(UrlGuardError):
        await url_guard.resolve_and_validate(host)


async def test_resolve_and_validate_returns_the_pin_for_a_public_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    assert await url_guard.resolve_and_validate("8.8.8.8") == "8.8.8.8"


async def test_resolve_and_validate_rejects_any_blocked_address_in_a_multi_address_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host resolving to several addresses is rejected if ANY is non-public —
    the rebinding case where one answer is public and another is internal."""
    _enable(monkeypatch)

    def fake_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UrlGuardError, match="non-public address"):
        await url_guard.resolve_and_validate("rebind.test")


async def test_resolve_and_validate_raises_when_resolution_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DNS failure surfaces as a loud guard error, never a silent pass."""
    _enable(monkeypatch)

    def failing_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", failing_getaddrinfo)
    with pytest.raises(UrlGuardError, match="cannot resolve host"):
        await url_guard.resolve_and_validate("nope.invalid")


async def test_resolve_and_validate_raises_when_no_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)

    def empty_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(socket, "getaddrinfo", empty_getaddrinfo)
    with pytest.raises(UrlGuardError, match="resolved to no addresses"):
        await url_guard.resolve_and_validate("empty.test")


async def test_allow_cidrs_opts_a_range_back_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    assert await url_guard.resolve_and_validate("127.0.0.1") == "127.0.0.1"
    with pytest.raises(UrlGuardError):
        await url_guard.resolve_and_validate("10.0.0.1")


async def test_blocks_cgnat_range_that_ipaddress_does_not_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """CGNAT (``100.64.0.0/10``) is not flagged by ``ipaddress``'s classifiers,
    so the guard's explicit blocked-CIDR list must reject it — and ``allow_cidrs``
    must still be able to opt it back in."""
    _enable(monkeypatch)
    with pytest.raises(UrlGuardError, match="non-public address"):
        await url_guard.resolve_and_validate("100.64.0.1")

    _enable(monkeypatch, allow_cidrs=["100.64.0.0/10"])
    assert await url_guard.resolve_and_validate("100.64.0.1") == "100.64.0.1"


@pytest.mark.parametrize(
    "host",
    [
        "64:ff9b::1",  # NAT64 well-known prefix (RFC 6052)
        "100::1",  # discard-only address block (RFC 6666)
        "198.18.0.1",  # benchmarking (RFC 2544)
        "192.0.0.1",  # IETF protocol assignments (RFC 6890)
    ],
)
async def test_blocks_special_use_ranges_ipaddress_does_not_flag(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    """These IETF special-use ranges are not flagged by ``ipaddress``'s
    classifiers, so the guard's explicit ``_BLOCKED_NETWORKS`` list must reject
    them — a deterministic IP-literal check that catches a typo in a blocked CIDR
    literal, especially the IPv6 ones."""
    _enable(monkeypatch)
    with pytest.raises(UrlGuardError, match="non-public address"):
        await url_guard.resolve_and_validate(host)


async def test_ipv4_mapped_ipv6_is_judged_on_its_ipv4_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """An IPv4-mapped IPv6 address (``::ffff:127.0.0.1``) is judged on its IPv4
    form, so a loopback address wearing an IPv6 mapping is still blocked."""
    _enable(monkeypatch)

    def mapped_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::ffff:127.0.0.1", 0, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", mapped_getaddrinfo)
    with pytest.raises(UrlGuardError, match="non-public address"):
        await url_guard.resolve_and_validate("mapped.test")


def test_guard_enabled_reflects_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    assert url_guard.guard_enabled() is True
    _disable(monkeypatch)
    assert url_guard.guard_enabled() is False


def test_enforce_size_caps_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, max_response_bytes=10)
    url_guard.enforce_size(10)
    with pytest.raises(UrlGuardError, match="exceeds max_response_bytes"):
        url_guard.enforce_size(11)


def test_enforce_size_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    url_guard.enforce_size(10**9)


def test_settings_use_the_tai_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard reads ``TAI_URL_GUARD_``-prefixed env, the hard-renamed prefix."""
    monkeypatch.setenv("TAI_URL_GUARD_ENABLED", "false")
    monkeypatch.setenv("TAI_URL_GUARD_MAX_RESPONSE_BYTES", "512")
    settings = UrlGuardSettings()
    assert settings.enabled is False
    assert settings.max_response_bytes == 512
