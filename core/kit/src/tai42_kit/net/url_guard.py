"""SSRF guard for server-side URL fetches.

Any caller that fetches a caller-supplied URL server-side — such as
:func:`tai42_kit.net.fetch_url` — could be steered by a poisoned page or document
at internal-only services or the cloud metadata endpoint (``169.254.169.254``).
The guard resolves each target host, refuses any address that is private,
loopback, link-local, reserved, multicast, or unspecified, and returns the
validated address to connect to so the same lookup that is checked is the one the
connection uses (closing DNS-rebinding). Callers apply this on the initial request
and every redirect hop, and cap the response size.

The guard is on by default and configured through :class:`UrlGuardSettings`
(env prefix ``TAI_URL_GUARD_``): set ``TAI_URL_GUARD_ALLOW_CIDRS`` to opt
specific ranges back in for a deployment that deliberately reaches internal
hosts, or ``TAI_URL_GUARD_ENABLED=false`` to turn it off entirely. Both are
explicit operator choices, never a silent bypass.
"""

from __future__ import annotations

import asyncio
import socket
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import TaiBaseSettings, settings_cache


class UrlGuardError(Exception):
    """Raised when a target host cannot be resolved or is rejected as non-public, or when a
    response exceeds the guard's size cap or its redirect chain exceeds ``max_redirects``."""


class UrlGuardSettings(TaiBaseSettings):
    """Policy for the SSRF guard on server-side URL fetches."""

    model_config = SettingsConfigDict(env_prefix="TAI_URL_GUARD_")

    enabled: bool = True
    # CIDR ranges an operator opts back in (e.g. ``["10.0.0.0/8"]``) despite being
    # otherwise-blocked private/internal space.
    allow_cidrs: list[str] = Field(default_factory=list)
    max_response_bytes: int = 100 * 1024 * 1024
    max_redirects: int = 20


@settings_cache
def url_guard_settings() -> UrlGuardSettings:
    """Return the process-wide :class:`UrlGuardSettings`, cached after first load."""
    return UrlGuardSettings()


def guard_enabled() -> bool:
    """Return whether the SSRF guard is enabled (``TAI_URL_GUARD_ENABLED``)."""
    return url_guard_settings().enabled


# Ranges Python's ``ipaddress`` classifiers do not flag but that must not be
# reachable server-side: CGNAT and IETF special-use blocks an SSRF target could
# hide behind (shared address space, documentation/test nets, benchmarking, NAT64,
# and the IPv6 discard-only prefix). Built once and checked alongside the
# classifier predicates; ``allow_cidrs`` still overrides them.
_BLOCKED_NETWORKS: tuple[IPv4Network | IPv6Network, ...] = tuple(
    ip_network(cidr)
    for cidr in (
        "100.64.0.0/10",  # CGNAT / shared address space (RFC 6598)
        "192.0.0.0/24",  # IETF protocol assignments (RFC 6890)
        "192.0.2.0/24",  # TEST-NET-1 (RFC 5737)
        "198.51.100.0/24",  # TEST-NET-2 (RFC 5737)
        "203.0.113.0/24",  # TEST-NET-3 (RFC 5737)
        "198.18.0.0/15",  # benchmarking (RFC 2544)
        "64:ff9b::/96",  # NAT64 well-known prefix (RFC 6052)
        "100::/64",  # discard-only address block (RFC 6666)
    )
)


def _is_blocked_address(ip_text: str, allow_nets: list[IPv4Network | IPv6Network]) -> bool:
    ip = ip_address(ip_text)
    # An IPv4-mapped IPv6 address (``::ffff:127.0.0.1``) is judged on its IPv4 form.
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip in net for net in allow_nets):
        return False
    if any(ip in net for net in _BLOCKED_NETWORKS):
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


async def resolve_and_validate(host: str) -> str:
    """Resolve ``host``, reject it if any resolved address is non-public, and
    return the first validated address to connect to (the "pin").

    Resolving once and connecting to the returned address closes DNS-rebinding:
    the same lookup that is validated is the one the connection uses, so an
    attacker cannot answer public during the check and internal at connect time.
    Raises :class:`UrlGuardError` if resolution fails or any address is blocked.
    """
    settings = url_guard_settings()
    allow_nets = [ip_network(cidr, strict=False) for cidr in settings.allow_cidrs]

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlGuardError(f"SSRF guard: cannot resolve host {host!r}: {exc}") from exc

    if not infos:
        raise UrlGuardError(f"SSRF guard: host {host!r} resolved to no addresses.")

    for info in infos:
        ip_text = str(info[4][0])
        if _is_blocked_address(ip_text, allow_nets):
            raise UrlGuardError(
                f"SSRF guard blocked host {host!r}: resolves to non-public address "
                f"{ip_text}. Set TAI_URL_GUARD_ALLOW_CIDRS to opt specific ranges in, or "
                f"TAI_URL_GUARD_ENABLED=false to disable the guard."
            )

    return str(infos[0][4][0])


def enforce_size(nbytes: int) -> None:
    """Raise :class:`UrlGuardError` when ``nbytes`` exceeds the configured cap.
    A no-op when the guard is disabled. Never truncates — an over-cap response is
    refused loudly."""
    settings = url_guard_settings()
    if not settings.enabled:
        return
    if nbytes > settings.max_response_bytes:
        raise UrlGuardError(
            f"SSRF guard: response body of {nbytes} bytes exceeds max_response_bytes={settings.max_response_bytes}."
        )
