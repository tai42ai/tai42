"""Proxy dispatcher installation and the per-call routing context: idempotent
install, plain-socket passthrough, and route isolation and restoration.
"""

import asyncio
import socket
from typing import cast

import pytest
from tests.extensions._proxy_support import (
    _http_route,
    _socks_route,
)

from tai42_toolbox._internal.extensions.socket_routing import (
    RouteConfig,
    RoutingSocket,
    active_route,
    install_dispatcher,
    route,
)


def test_install_dispatcher_is_idempotent():
    install_dispatcher()
    assert socket.socket is RoutingSocket
    install_dispatcher()
    assert socket.socket is RoutingSocket


def test_no_route_leaves_a_plain_socket():
    install_dispatcher()
    sock = socket.socket()
    try:
        assert type(sock) is RoutingSocket
        assert isinstance(sock, socket.socket)
        assert sock._route is None
    finally:
        sock.close()


def test_plain_socket_connects_directly_when_no_route_is_active():
    # With no active route, connect() is an ordinary direct connection — the
    # dispatcher installed process-wide never touches unrouted traffic.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        install_dispatcher()
        sock = cast(RoutingSocket, socket.socket())
        assert sock._route is None
        try:
            sock.connect(("127.0.0.1", port))
            conn, _ = listener.accept()
            conn.close()
        finally:
            sock.close()
    finally:
        listener.close()


def test_http_route_socket_isinstance_and_carries_config():
    cfg = _http_route()
    with route(cfg):
        sock = RoutingSocket()
    try:
        assert isinstance(sock, socket.socket)
        assert sock._route is cfg
    finally:
        sock.close()


def test_socks_route_dispatches_to_socksocket_with_validated_address():
    import socks

    # The validated address (not the hostname) is handed to PySocks' set_proxy, so
    # PySocks never re-resolves the hostname at connect (which would reopen rebinding).
    cfg = _socks_route(socks.SOCKS5, host="proxy.example", connect_address="203.0.113.5", rdns=True)
    with route(cfg):
        sock = RoutingSocket()
    try:
        assert isinstance(sock, socket.socket)
        assert isinstance(sock, socks.socksocket)
        proxy_type, addr, port, rdns, _user, _pw = sock.proxy
        assert proxy_type == socks.SOCKS5
        assert addr == "203.0.113.5"
        assert port == 1080
        assert rdns is True
    finally:
        sock.close()


def test_unrouted_call_is_not_proxied_during_a_routed_call():
    # While a routed call is in flight, a concurrent unrelated
    # task's socket must NOT be routed — it stays a plain, unrouted socket.
    install_dispatcher()
    cfg = _http_route()
    captured: dict[str, object] = {}

    async def run() -> None:
        routed_created = asyncio.Event()
        unrouted_done = asyncio.Event()

        async def routed() -> None:
            with route(cfg):
                sock = cast(RoutingSocket, socket.socket())
                captured["routed"] = sock._route
                routed_created.set()
                await unrouted_done.wait()
                captured["routed_after"] = sock._route
                sock.close()

        async def unrouted() -> None:
            await routed_created.wait()
            sock = cast(RoutingSocket, socket.socket())
            captured["unrouted"] = sock._route
            sock.close()
            unrouted_done.set()

        await asyncio.gather(routed(), unrouted())

    asyncio.run(run())
    assert captured["routed"] is cfg
    assert captured["unrouted"] is None
    assert captured["routed_after"] is cfg


def test_concurrent_routed_calls_each_keep_their_own_config():
    install_dispatcher()
    cfg_a = _http_route(host="proxy-a")
    cfg_b = _http_route(host="proxy-b")
    captured: dict[str, object] = {}

    async def run() -> None:
        both_created = asyncio.Barrier(2)

        async def routed(tag: str, cfg: RouteConfig) -> None:
            with route(cfg):
                sock = cast(RoutingSocket, socket.socket())
                # Hold both routes open simultaneously before reading either config.
                await both_created.wait()
                captured[tag] = sock._route
                sock.close()

        await asyncio.gather(routed("a", cfg_a), routed("b", cfg_b))

    asyncio.run(run())
    assert captured["a"] is cfg_a
    assert captured["b"] is cfg_b


def test_route_resets_on_success_and_on_exception():
    cfg = _http_route()
    with route(cfg):
        assert active_route.get() is cfg
    assert active_route.get() is None

    with pytest.raises(RuntimeError, match="boom"), route(cfg):
        raise RuntimeError("boom")
    # The route is reset even when the block raised.
    assert active_route.get() is None


def test_nested_route_restores_outer_on_inner_exit():
    # Leaving the inner block restores the OUTER route (not None), so nested proxied calls in one task keep routing.
    outer, inner = _http_route(host="outer"), _http_route(host="inner")
    with route(outer):
        with route(inner):
            assert active_route.get() is inner
        assert active_route.get() is outer
    assert active_route.get() is None
