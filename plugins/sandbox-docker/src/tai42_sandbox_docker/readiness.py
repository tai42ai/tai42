"""Pure helpers behind the Docker provider's egress-firewall readiness probe.

The provider proves, on the session-create path, that the engine's inner-bridge
egress firewall is in force before it returns a session: it runs a throwaway probe
container on the egress tier and dials two DERIVED targets — a DENY target the
firewall must drop and an ALLOW target it must leave reachable. These are the pure
parse/predicate/derivation helpers behind that probe; the orchestration (container
create, exec, teardown, and the one-probe-per-burst lock) lives on the provider.
"""

from __future__ import annotations

import socket
from urllib.parse import urlsplit

from tai42_contract.sandbox import SandboxError

# aiodocker re-types an aiohttp connection failure raised mid-request as a
# ``DockerError`` with this status; it is the observable that the engine daemon went
# away and readiness must be re-proven before the next create.
ENGINE_UNREACHABLE_STATUS = 900

# The engine control API's TLS port, used when the configured endpoint names no port.
DEFAULT_CONTROL_PORT = 2376

# Every DNS resolver listens here; the ALLOW target is the probe container's own
# resolver on this port.
RESOLVER_PORT = 53

# ``nc``'s connect-wait: a firewall-DROPPED destination exits nonzero only once this
# elapses, so a drop is distinguishable from an immediate connection refusal.
PROBE_DIAL_WAIT_SECONDS = 4

# The host-side bound on a single probe ``exec`` (resolver read or one dial). The
# whole probe carries its own overall timeout on top of this per-step ceiling.
PROBE_EXEC_TIMEOUT_SECONDS = PROBE_DIAL_WAIT_SECONDS + 20


def is_engine_unreachable(status: int) -> bool:
    """Whether an engine ``DockerError`` status is aiodocker's connection-failure
    code — the signal that the daemon went away."""
    return status == ENGINE_UNREACHABLE_STATUS


def engine_control_address(host: str) -> tuple[str, int]:
    """The engine control endpoint the provider itself dials, as ``(host, port)``.

    This is the readiness DENY target: a session on the egress tier must NOT reach the
    engine's own control address across the firewall. A local-socket endpoint
    (``unix://`` / ``npipe://`` / a bare path) has no network address to probe, so an
    enabled probe against one is refused loudly rather than run against a target that
    does not exist."""
    if host.startswith(("unix://", "npipe://", "/")):
        raise SandboxError(
            "the egress-firewall readiness probe needs a network engine endpoint to derive its deny "
            f"target, but SANDBOX_DOCKER_HOST {host!r} is a local socket"
        )
    parts = urlsplit(host if "://" in host else f"//{host}")
    if not parts.hostname:
        raise SandboxError(f"cannot derive the readiness-probe deny target: SANDBOX_DOCKER_HOST {host!r} names no host")
    return parts.hostname, parts.port or DEFAULT_CONTROL_PORT


def resolve_ipv4(host: str) -> str:
    """Resolve ``host`` to one IPv4 address for the deny-target dial.

    The probe hands the SESSION an address, not a name: an inner egress container does
    not share the app's DNS view of the control network, so the app resolves the
    engine host here. A host that resolves to no address is a loud refusal, never a
    silent skip."""
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SandboxError(
            f"cannot resolve the engine host {host!r} to an address for the readiness-probe deny target"
        ) from exc
    for info in infos:
        return str(info[4][0])
    raise SandboxError(f"the engine host {host!r} resolved to no address for the readiness-probe deny target")


def resolver_from_resolv_conf(resolv_conf: str) -> str:
    """The probe container's own DNS resolver — the first ``nameserver`` in its
    ``/etc/resolv.conf`` — the readiness ALLOW target.

    On the rootless-dind engine this address sits inside the private range the firewall
    drops and stays reachable only because the firewall accepts the daemon's own subnets
    above those drops, so a reachable resolver is a statement about that ordering. A
    resolv.conf carrying no nameserver cannot yield a sound allow target, so the probe
    refuses loudly rather than probe a target it could not derive."""
    for line in resolv_conf.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            return fields[1]
    raise SandboxError(
        "cannot derive the readiness-probe allow target: the probe container's /etc/resolv.conf carries no nameserver"
    )
