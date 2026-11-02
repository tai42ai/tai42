"""The one reading of ``SANDBOX_DOCKER_HOST``: the endpoint both engine clients dial."""

from __future__ import annotations

import re
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import aiohttp  # pyright: ignore[reportMissingImports]
import pytest
import trustme
from aiohttp import web
from tai42_contract.sandbox import SandboxError

from tai42_sandbox_docker.engine_endpoint import (
    EngineEndpoint,
    TcpEndpoint,
    UnixSocketEndpoint,
    resolve_engine_endpoint,
    rest_connector,
)
from tai42_sandbox_docker.provider import DockerSandbox
from tai42_sandbox_docker.settings import DockerSandboxSettings


@pytest.mark.parametrize(
    ("host", "tls", "endpoint"),
    [
        ("unix:///var/run/docker.sock", True, UnixSocketEndpoint(path="/var/run/docker.sock")),
        ("/var/run/docker.sock", False, UnixSocketEndpoint(path="/var/run/docker.sock")),
        ("tcp://engine:2376", True, TcpEndpoint(url="https://engine:2376", host="engine", port=2376)),
        ("http://engine:2376", True, TcpEndpoint(url="https://engine:2376", host="engine", port=2376)),
        ("https://engine:2376", True, TcpEndpoint(url="https://engine:2376", host="engine", port=2376)),
        ("tcp://engine:2375", False, TcpEndpoint(url="tcp://engine:2375", host="engine", port=2375)),
        # A network host naming no port is dialled on the engine's default: 2376 over TLS, 2375 plain.
        ("tcp://engine", True, TcpEndpoint(url="https://engine:2376", host="engine", port=2376)),
        ("https://engine", True, TcpEndpoint(url="https://engine:2376", host="engine", port=2376)),
        ("tcp://engine", False, TcpEndpoint(url="tcp://engine:2375", host="engine", port=2375)),
        ("http://engine", False, TcpEndpoint(url="http://engine:2375", host="engine", port=2375)),
        ("https://e.example", False, TcpEndpoint(url="https://e.example:2376", host="e.example", port=2376)),
        ("tcp://[::1]", True, TcpEndpoint(url="https://[::1]:2376", host="::1", port=2376)),
    ],
)
def test_the_host_names_one_endpoint(host: str, tls: bool, endpoint: object) -> None:
    assert resolve_engine_endpoint(host, tls=tls) == endpoint


_ACCEPTED_FORMS = r"name a unix socket \(unix:// or a bare path\) or a network engine \(tcp://, http:// or https://\)"


@pytest.mark.parametrize("tls", [True, False])
def test_a_named_pipe_host_is_refused(tls: bool) -> None:
    with pytest.raises(
        SandboxError,
        match=r"'npipe:////\./pipe/docker_engine' names a Windows named pipe, which the Docker sandbox "
        r"does not dial: " + _ACCEPTED_FORMS,
    ):
        resolve_engine_endpoint("npipe:////./pipe/docker_engine", tls=tls)


@pytest.mark.parametrize("tls", [True, False])
def test_a_unix_host_with_no_socket_path_is_refused(tls: bool) -> None:
    with pytest.raises(
        SandboxError,
        match=r"'unix://' names no socket path: " + _ACCEPTED_FORMS,
    ):
        resolve_engine_endpoint("unix://", tls=tls)


@pytest.mark.parametrize("tls", [True, False])
@pytest.mark.parametrize(
    "host",
    ["TCP://engine", "Tcp://engine:2376", "UNIX:///var/run/docker.sock", "fd://engine", "ssh://engine", "engine:2376"],
)
def test_a_host_of_any_other_form_is_refused(host: str, tls: bool) -> None:
    with pytest.raises(
        SandboxError,
        match=re.escape(repr(host)) + r" names no endpoint the Docker sandbox dials: " + _ACCEPTED_FORMS,
    ):
        resolve_engine_endpoint(host, tls=tls)


@pytest.mark.parametrize("tls", [True, False])
@pytest.mark.parametrize(
    ("host", "port"),
    [("tcp://engine:", ""), ("tcp://engine:abc", "abc"), ("https://engine:99999", "99999"), ("tcp://[::1]:x1", "x1")],
)
def test_a_network_host_naming_a_malformed_port_is_refused(host: str, port: str, tls: bool) -> None:
    with pytest.raises(
        SandboxError,
        match=re.escape(f"{host!r} names the port {port!r}, which is not a port number from 0 to 65535: ")
        + _ACCEPTED_FORMS,
    ):
        resolve_engine_endpoint(host, tls=tls)


@pytest.mark.parametrize("tls", [True, False])
@pytest.mark.parametrize("host", ["tcp://", "https://", "tcp://:2376", "tcp://user@:2376"])
def test_a_network_host_naming_no_host_is_refused(host: str, tls: bool) -> None:
    with pytest.raises(SandboxError, match=re.escape(f"{host!r} names no host: ") + _ACCEPTED_FORMS):
        resolve_engine_endpoint(host, tls=tls)


@pytest.mark.parametrize("tls", [True, False])
@pytest.mark.parametrize("host", ["tcp://[::1:2376", "https://[::1", "tcp://engine]:2376"])
def test_a_network_host_that_is_not_a_url_is_refused(host: str, tls: bool) -> None:
    with pytest.raises(SandboxError, match=re.escape(f"{host!r} is not a URL (") + r"[^)]+\): " + _ACCEPTED_FORMS):
        resolve_engine_endpoint(host, tls=tls)


@pytest.mark.parametrize(
    ("endpoint", "base_url"),
    [
        (UnixSocketEndpoint(path="/run/docker.sock"), "unix://localhost"),
        (TcpEndpoint(url="https://engine:2376", host="engine", port=2376), "https://engine:2376"),
    ],
)
def test_the_rest_client_composes_its_requests_on_the_endpoints_base_url(endpoint: object, base_url: str) -> None:
    assert cast(EngineEndpoint, endpoint).rest_base_url == base_url


async def test_a_unix_socket_is_dialed_by_its_path() -> None:
    connector = rest_connector(UnixSocketEndpoint(path="/run/docker.sock"), ssl_context=None)
    try:
        assert isinstance(connector, aiohttp.UnixConnector)
        assert connector.path == "/run/docker.sock"
    finally:
        await connector.close()


class _ConnectorRecorder:
    def __init__(self, path: str | None = None, *, ssl: object = None) -> None:
        self.path = path
        self.ssl = ssl


@pytest.mark.parametrize("with_identity", [True, False])
def test_a_network_endpoint_is_dialed_with_its_client_identity_or_the_default_trust(
    monkeypatch: pytest.MonkeyPatch, with_identity: bool
) -> None:
    monkeypatch.setattr(aiohttp, "TCPConnector", _ConnectorRecorder)
    context = ssl.create_default_context() if with_identity else None
    connector = rest_connector(TcpEndpoint(url="https://engine:2376", host="engine", port=2376), ssl_context=context)
    assert cast(_ConnectorRecorder, connector).ssl is (context if with_identity else True)


@pytest.mark.parametrize(
    ("host", "tls", "address", "authority"),
    [
        ("tcp://engine:2376", True, ("engine", 2376), "engine:2376"),
        ("tcp://10.0.0.5:2375", False, ("10.0.0.5", 2375), "10.0.0.5:2375"),
        ("https://engine", True, ("engine", 2376), "engine:2376"),
        ("tcp://[::1]", False, ("::1", 2375), "[::1]:2375"),
    ],
)
def test_a_network_endpoint_names_its_address_and_whether_it_runs_tls(
    host: str, tls: bool, address: tuple[str, int], authority: str
) -> None:
    endpoint = resolve_engine_endpoint(host, tls=tls)
    assert isinstance(endpoint, TcpEndpoint)
    assert (endpoint.tls, endpoint.address(), endpoint.authority) == (tls, address, authority)


@pytest.fixture
async def plain_engine() -> AsyncIterator[int]:
    """A plain-HTTP engine on the loopback address answering its version, unversioned and versioned; yields its port."""

    async def version(_: web.Request) -> web.Response:
        return web.json_response({"ApiVersion": "1.43"})

    app = web.Application()
    app.router.add_get("/version", version)
    app.router.add_get("/v1.43/version", version)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    yield runner.addresses[0][1]
    await runner.cleanup()


async def test_the_rest_client_takes_tls_from_the_settings_never_from_the_environment(
    plain_engine: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A docker-machine environment asking for TLS, with a client identity it could load.
    ca = trustme.CA()
    identity = ca.issue_cert("client")
    ca.cert_pem.write_to_path(tmp_path / "ca.pem")
    identity.cert_chain_pems[0].write_to_path(tmp_path / "cert.pem")
    identity.private_key_pem.write_to_path(tmp_path / "key.pem")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", str(tmp_path))
    sandbox = DockerSandbox(settings=DockerSandboxSettings(host=f"tcp://127.0.0.1:{plain_engine}", tls_verify=False))

    client = await sandbox._engine()
    try:
        assert await client.version() == {"ApiVersion": "1.43"}
    finally:
        await client.close()
        await client.connector.close()
