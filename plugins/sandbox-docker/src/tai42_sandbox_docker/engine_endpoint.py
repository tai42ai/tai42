"""Where the Docker engine listens: the one reading of ``SANDBOX_DOCKER_HOST``.

The provider dials the engine through two clients: aiodocker for every REST call, and
its own exec attachment (:mod:`~tai42_sandbox_docker.attach`) for the hijacked stream of
an exec. Both read the configured host through :func:`resolve_engine_endpoint` and take
their TLS from the provider's settings alone, never from the environment, so they always
reach the same engine the same way: the REST client dials every endpoint kind through the
connector :func:`rest_connector` builds for it, so aiodocker never picks a socket or a TLS
context of its own. The egress-firewall readiness probe takes its deny target from
the same endpoint, so it checks the very address both clients dial.
"""

from __future__ import annotations

import re
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp  # pyright: ignore[reportMissingImports]
from tai42_contract.sandbox import SandboxError

# A ``tcp://``/``http://`` scheme carrying an mTLS control API. aiodocker composes its
# requests on the URL it is handed as given (the connector carries the TLS), so a
# ``tcp://`` URL would dial PLAINTEXT against the TLS port — the daemon then
# answers "Client sent an HTTP request to an HTTPS server". The endpoint normalizes
# the scheme to ``https://`` so an operator's canonical ``tcp://`` endpoint speaks
# mTLS without them having to write ``https://``.
_TLS_SCHEME_RX = re.compile(r"^(?:tcp|http)://")

# The schemes that name a network engine, matched exactly as written.
_NETWORK_SCHEMES = frozenset({"tcp", "http", "https"})

# The port a network host that names none is dialed on: the Docker engine's own defaults,
# its TLS port for an endpoint dialed over TLS and its plain port for one dialed plain.
_DEFAULT_TLS_PORT = 2376
_DEFAULT_PLAIN_PORT = 2375

# The highest TCP port number.
_MAX_PORT = 65535


@dataclass(frozen=True)
class UnixSocketEndpoint:
    """An engine on a local unix socket at ``path``; it carries no client identity."""

    path: str

    @property
    def rest_base_url(self) -> str:
        """The base URL the REST client composes its requests on; the connector picks the socket."""
        return "unix://localhost"


@dataclass(frozen=True)
class TcpEndpoint:
    """An engine on a network address: ``url`` addresses it, and names ``host`` and ``port``.

    The REST client composes its requests on ``url``; the exec attachment and the readiness
    probe dial ``(host, port)``, the address ``url`` names.
    """

    url: str
    host: str
    port: int

    @property
    def rest_base_url(self) -> str:
        """The base URL the REST client composes its requests on: the endpoint's own URL."""
        return self.url

    @property
    def tls(self) -> bool:
        """Whether the engine is dialed over TLS: an ``https://`` URL."""
        return _dials_tls(self.url)

    def address(self) -> tuple[str, int]:
        """The ``(host, port)`` the engine listens on."""
        return self.host, self.port

    @property
    def authority(self) -> str:
        """The ``host[:port]`` part of the URL, as a request to the engine names it."""
        return urlsplit(self.url).netloc


EngineEndpoint = UnixSocketEndpoint | TcpEndpoint


def resolve_engine_endpoint(host: str, *, tls: bool) -> EngineEndpoint:
    """The endpoint ``host`` names.

    ``unix://`` and a bare filesystem path name a local unix socket, which carries no mTLS
    client identity, whatever ``tls`` says; a ``unix://`` that names no socket path raises
    :class:`SandboxError`. ``tcp://``, ``http://`` and ``https://`` name a
    network endpoint: under mTLS (``tls`` true) a ``tcp://`` / ``http://`` scheme is
    normalized to ``https://`` so both clients run TLS over it; without TLS the scheme is
    left as given. A network host that names no port is given the engine's default, 2376
    for an endpoint dialed over TLS (every host under mTLS, and any ``https://`` host) and
    2375 for a plain ``tcp://`` or ``http://`` dial, so every client dials the same address;
    one that names a port that is not a number from 0 to 65535, an empty one included, names
    no host, or is not a URL at all (an unclosed IPv6 bracket) raises :class:`SandboxError`.

    Schemes are matched exactly as written. Any other host — a Windows named pipe
    (``npipe://``), another scheme (``ssh://``, ``fd://``), a differently-cased scheme
    (``TCP://``) or no scheme at all — raises :class:`SandboxError`: the provider dials
    none of them.
    """
    if host.startswith("/"):
        return UnixSocketEndpoint(path=host)
    scheme, separator, rest = host.partition("://")
    if separator and scheme == "unix":
        if not rest:
            raise _undialable_host(host, "names no socket path")
        return UnixSocketEndpoint(path=rest)
    if separator and scheme in _NETWORK_SCHEMES:
        return _network_endpoint(host, tls=tls)
    if separator and scheme == "npipe":
        raise _undialable_host(host, "names a Windows named pipe, which the Docker sandbox does not dial")
    raise _undialable_host(host, "names no endpoint the Docker sandbox dials")


def _network_endpoint(host: str, *, tls: bool) -> TcpEndpoint:
    """The endpoint the network ``host`` names, a ``tcp://``, ``http://`` or ``https://`` URL.

    Its port is the one it names, else the engine's default for whether it is dialed over
    TLS; a host that is not a URL, names
    a port that is not a number from 0 to 65535, or names no host raises :class:`SandboxError`.
    """
    try:
        parts = urlsplit(host)
    except ValueError as exc:
        raise _undialable_host(host, f"is not a URL ({exc})") from exc
    port = _port_as_written(parts.netloc)
    if port is not None and not (port.isascii() and port.isdigit() and int(port) <= _MAX_PORT):
        raise _undialable_host(host, f"names the port {port!r}, which is not a port number from 0 to {_MAX_PORT}")
    if not parts.hostname:
        raise _undialable_host(host, "names no host")
    url = _TLS_SCHEME_RX.sub("https://", host) if tls else host
    if port is not None:
        return TcpEndpoint(url=url, host=parts.hostname, port=int(port))
    default_port = _DEFAULT_TLS_PORT if _dials_tls(url) else _DEFAULT_PLAIN_PORT
    url = urlsplit(url)._replace(netloc=f"{parts.netloc}:{default_port}").geturl()
    return TcpEndpoint(url=url, host=parts.hostname, port=default_port)


def _dials_tls(url: str) -> bool:
    """Whether the network engine ``url`` is dialed over TLS: an ``https://`` URL."""
    return urlsplit(url).scheme == "https"


def _undialable_host(host: str, reason: str) -> SandboxError:
    """The refusal of a ``SANDBOX_DOCKER_HOST`` the provider does not dial, naming the forms it does."""
    return SandboxError(
        f"cannot dial the Docker engine: SANDBOX_DOCKER_HOST {host!r} {reason}: name a unix socket "
        "(unix:// or a bare path) or a network engine (tcp://, http:// or https://)"
    )


def _port_as_written(netloc: str) -> str | None:
    """The port ``netloc`` names, as written (``""`` for a bare ``:``), or ``None`` when it names none.

    The host part an IPv6 literal's brackets enclose holds colons of its own, never a port.
    """
    hostinfo = netloc.rpartition("@")[2]
    _, bracket, after_literal = hostinfo.rpartition("]")
    _, colon, port = (after_literal if bracket else hostinfo).rpartition(":")
    return port if colon else None


def rest_connector(endpoint: EngineEndpoint, *, ssl_context: ssl.SSLContext | None) -> aiohttp.BaseConnector:
    """The aiohttp connector the REST client dials ``endpoint`` through.

    A unix socket is dialed by its path and carries no client identity. A network endpoint
    is dialed over TCP with ``ssl_context`` (the mTLS client identity) when there is one, and
    otherwise with the default trust, which an ``https://`` URL runs TLS under and a plain one
    never uses.
    """
    if isinstance(endpoint, UnixSocketEndpoint):
        return aiohttp.UnixConnector(endpoint.path)
    return aiohttp.TCPConnector(ssl=ssl_context if ssl_context is not None else True)
