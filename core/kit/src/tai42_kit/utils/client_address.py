"""Which client is this request from — the ONE resolver every consumer reads.

``X-Forwarded-For`` is caller-writable text: any client can claim any address in
it. It is therefore believed only under a declared trust statement, and the
statement comes in exactly two shapes, checked in this order:

1. ``trusted_hops = N`` (a COUNT). The deployment states that exactly ``N``
   proxies sit in front of it, so the ``N`` right-most entries of the chain
   (``X-Forwarded-For`` entries followed by the socket peer) are those proxies and
   the entry immediately left of them is the client. A count survives a proxy
   renumbering, which is why it WINS over the address roster: when both are set,
   ``trusted_proxies`` is not consulted for the walk.
2. ``trusted_proxies`` (ADDRESSES, single hosts or CIDR blocks). The socket peer
   must itself be trusted before a single header byte is read; then the walk runs
   right-to-left and the first entry that is not itself trusted is the client.

Anything else FAILS CLOSED to the socket address: an untrusted peer, a chain too
short for the declared hop count, or an unparseable entry at the position the walk
selected. Falling back is never quiet — a malformed chain would otherwise collapse
every caller into one shared bucket, so it is reported (throttled to one line a
minute with the suppressed count, so a hostile header cannot flood the log).

The returned address is normalised so one client cannot hold two identities: an
IPv4-mapped IPv6 address (``::ffff:a.b.c.d``) unwraps to its IPv4, a ``%zone``
identifier is dropped, and a ``[v6]:port`` / ``v4:port`` entry (some managed load
balancers append the source port) reduces to its address. :func:`bucket_of`
additionally collapses an IPv6 address to its /64, since a single host routinely
holds a whole /64.

The resolver reads two primitives — the socket peer and the raw ``X-Forwarded-For``
header — so every caller (the app rate limiter, a channel door) resolves one
deployment's trust statement one way, none importing another's HTTP layer.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections.abc import Sequence
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import TaiBaseSettings, settings_cache

logger = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# The address recorded when the ASGI server names no peer (an in-process or unix
# transport). One shared bucket, but never caller-written text.
UNKNOWN_CLIENT = "unknown"

# The forwarded-for header name, lower-cased for case-insensitive header lookup. The
# single source every consumer reads the raw chain from.
XFF_HEADER = "x-forwarded-for"

# Longest textual IPv6 address; a rejected entry is logged truncated to it so a
# multi-kilobyte header cannot bloat the trail.
_MAX_LOGGED_ENTRY = 45

# A rejected chain is reported at most once per this many seconds, carrying the
# count suppressed since the last line — loud enough to see, bounded enough that a
# hostile header cannot flood the log.
_REJECT_WARNING_INTERVAL_SECONDS = 60.0

_last_reject_warning = 0.0
_suppressed_rejects = 0

# Once-per-process guard for the "a proxy is in front and undeclared" evidence
# warning (an ``X-Forwarded-For`` arriving while no trust is declared).
_UNDECLARED_PROXY_WARNED = False


def _bare_address(raw: str) -> str:
    """One chain entry reduced to bare address text: ``[v6]``/``[v6]:port`` brackets
    and a ``v4:port`` suffix removed, a ``%zone`` identifier dropped. A bare IPv6
    address always carries at least two colons, so the single-colon test can never
    cut one in half."""
    text = raw.strip()
    if text.startswith("["):
        end = text.find("]")
        if end != -1:
            text = text[1:end]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    zone = text.find("%")
    if zone != -1:
        text = text[:zone]
    return text


def parse_address(raw: str) -> IPAddress | None:
    """The normalised address an entry names, or ``None`` when it names none.

    An IPv4-mapped IPv6 address unwraps to the IPv4 it carries, so a dual-stack
    listener buckets a mapped client as itself and a mapped peer matches an IPv4
    CIDR in the trusted roster."""
    try:
        ip = ipaddress.ip_address(_bare_address(raw))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def parse_trusted_networks(entries: Sequence[str]) -> list[IPNetwork]:
    """The declared trusted-proxy roster as networks — a single address and a CIDR
    block share one containment code path (``198.51.100.7`` parses as
    ``198.51.100.7/32``). Raises on an entry that is neither, so a typo'd roster is
    refused where it is configured instead of silently trusting nothing."""
    networks: list[IPNetwork] = []
    for entry in entries:
        text = entry.strip()
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise ValueError(f"trusted proxy {entry!r} is neither an IP address nor a CIDR block: {exc}") from exc
    return networks


@lru_cache(maxsize=32)
def _networks(entries: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    """:func:`parse_trusted_networks` memoized on the declared roster, so the
    per-request trust test parses nothing."""
    return tuple(parse_trusted_networks(entries))


def _is_trusted(ip: IPAddress, networks: Sequence[IPNetwork]) -> bool:
    """Containment against the roster. A network of the other IP version answers
    ``False`` rather than raising, so a mixed v4/v6 roster needs no branching."""
    return any(ip in network for network in networks)


def _warn_rejected_chain(reason: str, entry: str) -> None:
    """Report a chain the resolver refused to believe (the caller then buckets by the
    socket address). Throttled to one line per interval, carrying the count
    suppressed since the last one — never silent, never floodable."""
    global _last_reject_warning, _suppressed_rejects
    now = time.monotonic()
    if now - _last_reject_warning < _REJECT_WARNING_INTERVAL_SECONDS:
        _suppressed_rejects += 1
        return
    suppressed = _suppressed_rejects
    _suppressed_rejects = 0
    _last_reject_warning = now
    logger.warning(
        "client address: %s (entry %r) — falling back to the socket address; %d further such requests "
        "suppressed since the last line",
        reason,
        entry[:_MAX_LOGGED_ENTRY],
        suppressed,
    )


def _warn_undeclared_proxy_once() -> None:
    """Report the first ``X-Forwarded-For`` that arrives while no proxy trust is
    declared — the live evidence that either a proxy sits in front undeclared (every
    client then shares the proxy's bucket) or a client is spoofing the header. Once
    per process: the posture, not the request, is the news."""
    global _UNDECLARED_PROXY_WARNED
    if _UNDECLARED_PROXY_WARNED:
        return
    _UNDECLARED_PROXY_WARNED = True
    logger.warning(
        "client address: an X-Forwarded-For header arrived but no proxy trust is declared, so it is IGNORED "
        "and the socket peer is the client. If a reverse proxy or load balancer fronts this deployment, every "
        "client is sharing ONE bucket — declare it with TAI_RATE_LIMIT_TRUSTED_PROXIES (addresses or CIDR "
        "blocks) or TAI_RATE_LIMIT_TRUSTED_HOPS (a hop count). If nothing fronts it, a client is spoofing the "
        "header and ignoring it is correct."
    )


def warn_if_proxy_trust_undeclared(log: logging.Logger, trusted_proxies: Sequence[str], trusted_hops: int) -> None:
    """Emit the boot-time posture line when a deployment declares NO proxy trust:
    every ``X-Forwarded-For`` is then ignored, which is correct for a directly
    exposed deployment and collapses the whole world into one bucket behind a proxy.
    Stated at boot so the posture is visible before the first flood makes it look
    like the limiter works."""
    if trusted_proxies or trusted_hops:
        return
    log.warning(
        "rate limiting: no proxy trust declared (TAI_RATE_LIMIT_TRUSTED_PROXIES / TAI_RATE_LIMIT_TRUSTED_HOPS "
        "are unset), so X-Forwarded-For is ignored and the socket peer is the client. Correct for a directly "
        "exposed deployment; behind a reverse proxy or load balancer it buckets EVERY client together."
    )


def _hop_mode_address(chain: list[str], trusted_hops: int, socket_address: str) -> str:
    """The client named by a fixed hop count: drop the ``trusted_hops`` right-most
    entries and take the next. A chain too short to hold that many proxies, or a
    selected entry that names no address, is a broken statement — report it and fall
    back to the socket address rather than believe a hop the count did not cover."""
    if len(chain) <= trusted_hops:
        _warn_rejected_chain(
            f"the forwarded chain holds {len(chain)} entries but trusted_hops={trusted_hops} needs at least "
            f"{trusted_hops + 1}",
            ", ".join(chain),
        )
        return socket_address
    candidate = chain[-(trusted_hops + 1)]
    ip = parse_address(candidate)
    if ip is None:
        _warn_rejected_chain(f"the entry {trusted_hops} hops from the socket names no IP address", candidate)
        return socket_address
    return str(ip)


def _address_mode_address(
    entries: list[str], networks: Sequence[IPNetwork], peer: IPAddress | None, socket_address: str
) -> str:
    """The client named by the trusted-address roster. The socket peer must itself be
    trusted before the header is read at all; then the right-most entry that is not
    itself a trusted proxy is the client. An unparseable entry the walk REACHES stops
    it (believing the next one left would let a broken proxy hand the whole chain to
    a caller): report and fall back. A chain whose every entry is trusted names no
    client beyond the peer, which is the socket address."""
    if peer is None or not _is_trusted(peer, networks):
        if entries and not networks:
            _warn_undeclared_proxy_once()
        return socket_address
    for entry in reversed(entries):
        ip = parse_address(entry)
        if ip is None:
            _warn_rejected_chain("a forwarded entry names no IP address", entry)
            return socket_address
        if not _is_trusted(ip, networks):
            return str(ip)
    return socket_address


def resolve_client_address(
    peer: str | None,
    forwarded_for: str,
    trusted_proxies: Sequence[str],
    trusted_hops: int,
) -> str:
    """The client address for one request (module docstring holds the trust rules and
    their precedence). ``peer`` is the socket address the ASGI server reports;
    ``forwarded_for`` is the raw ``X-Forwarded-For`` header value, believed only
    under a declared trust statement and never otherwise read."""
    peer_ip = parse_address(peer) if peer else None
    socket_address = str(peer_ip) if peer_ip is not None else (peer or UNKNOWN_CLIENT)
    entries = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]

    if trusted_hops > 0:
        # The count covers the socket peer too: it is the right-most proxy in front.
        return _hop_mode_address([*entries, socket_address], trusted_hops, socket_address)
    return _address_mode_address(entries, _networks(tuple(trusted_proxies)), peer_ip, socket_address)


def bucket_of(address: str) -> str:
    """The rate-limit bucket key for a resolved client address: an IPv6 address
    collapses to its /64 prefix (a single host routinely holds a whole /64); an IPv4
    address and the unknown-peer marker are the key as they stand."""
    ip = parse_address(address)
    if ip is None:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
    return str(ip)


class ClientTrustSettings(TaiBaseSettings):
    """The deployment's proxy-trust statement for :func:`resolve_client_address`.

    The env prefix is ``TAI_RATE_LIMIT_`` because the statement is one deployment
    fact read by both the app rate limiter and the channel doors' turn caps — one
    posture, one place, so the accountable client is resolved the same way for every
    consumer."""

    model_config = SettingsConfigDict(env_prefix="TAI_RATE_LIMIT_")

    # Reverse proxies whose X-Forwarded-For may be trusted for the client-address
    # resolution — single addresses, CIDR blocks, or both. Empty (default) = trust no
    # proxy: the direct peer is the client and the header is never read.
    trusted_proxies: list[str] = Field(default_factory=list)

    # The number of proxies known to sit in front of this deployment. Set it when the
    # proxy's own address moves (a managed load balancer renumbers): the resolver then
    # skips exactly this many right-most hops instead of matching addresses. A
    # positive value WINS over ``trusted_proxies``, which is then not consulted.
    trusted_hops: int = Field(default=0, ge=0)

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_trusted_proxies(cls, value: list[str]) -> list[str]:
        """Refuse a roster entry that is neither an address nor a CIDR block here,
        where the operator can see it — an unparseable entry would otherwise silently
        trust nothing and bucket every client behind the proxy together."""
        parse_trusted_networks(value)
        return value


@settings_cache
def client_trust_settings() -> ClientTrustSettings:
    return ClientTrustSettings()


def client_address(peer: str | None, forwarded_for: str) -> str:
    """The client address behind a request, resolved against the deployment's declared
    proxy trust. The single entry point: every consumer that needs to know who a
    request is from calls THIS with the socket peer and the raw ``X-Forwarded-For``,
    so one deployment's trust statement is read one way."""
    settings = client_trust_settings()
    return resolve_client_address(peer, forwarded_for, settings.trusted_proxies, settings.trusted_hops)


def client_bucket(peer: str | None, forwarded_for: str) -> str:
    """:func:`client_address` collapsed to its rate-limit bucket key."""
    return bucket_of(client_address(peer, forwarded_for))
