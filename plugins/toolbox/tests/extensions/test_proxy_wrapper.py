"""The ``proxy`` tool wrapper: registration, dispatcher install, composed signature,
and routing a wrapped tool call with reset afterwards.
"""

import asyncio
import inspect
import socket

import pytest
from tai42_contract.extensions import ExtensionKind
from tests.extensions._proxy_support import (
    _settings,
    _tool,
)

import tai42_toolbox.extensions.proxy as proxy_module
from tai42_toolbox._internal.extensions.socket_routing import (
    RouteConfig,
    RoutingSocket,
    active_route,
)
from tai42_toolbox.extensions.proxy import proxy


def test_registers_as_wrapper_named_proxy_requiring_body_locality(capture_registration):
    # ``requires_body_locality=True``: routing works only in the process running the tool body,
    # so the bind engine must place it inside any execution-relocating extension.
    assert capture_registration(proxy_module) == [("proxy", ExtensionKind.WRAPPER, True)]


def test_reserved_params_value():
    assert proxy.reserved_params == frozenset({"proxies"})


def test_factory_installs_the_dispatcher():
    proxy(_tool, "tool", "desc")
    assert socket.socket is RoutingSocket


def test_composed_signature_is_original_plus_proxies():
    original = inspect.signature(_tool)
    composed = inspect.signature(proxy(_tool, "tool", "desc"))

    original_names = set(original.parameters)
    composed_names = set(composed.parameters)

    assert original_names <= composed_names
    assert composed_names - original_names == {"proxies"}
    assert composed.parameters["proxies"].kind is inspect.Parameter.KEYWORD_ONLY
    assert composed.parameters["text"].annotation is str


def test_wraps_a_tool_ending_in_var_keyword():
    def tool(text: str, **kwargs: object) -> str:
        return text

    composed = inspect.signature(proxy(tool, "tool", "desc"))
    names = list(composed.parameters)

    assert names[-1] == "kwargs"
    assert composed.parameters["proxies"].kind is inspect.Parameter.KEYWORD_ONLY


def test_wrapper_runs_tool_with_route_active_and_resets_after(monkeypatch):
    _settings(monkeypatch, pool=["http://pool-proxy.example:8080"])
    captured: dict[str, object] = {}

    async def tool(text: str) -> str:
        captured["route"] = active_route.get()
        return text

    wrapped = proxy(tool, "tool", "desc")
    result = asyncio.run(wrapped("hi"))

    assert result == "hi"
    assert isinstance(captured["route"], RouteConfig)
    assert captured["route"].proxy_host == "pool-proxy.example"
    # The route is reset once the call returns.
    assert active_route.get() is None


def test_wrapper_raises_out_of_pool_url_and_routes_nothing(monkeypatch):
    _settings(monkeypatch, pool=["http://trusted.example:8080"])
    ran = {"tool": False}

    async def tool(text: str) -> str:
        ran["tool"] = True
        return text

    wrapped = proxy(tool, "tool", "desc")
    with pytest.raises(ValueError, match="not in the operator pool"):
        asyncio.run(wrapped("hi", proxies=["http://attacker.example:8080"]))
    assert ran["tool"] is False


def test_wrapper_supports_sync_tools(monkeypatch):
    _settings(monkeypatch, pool=["http://pool-proxy.example:8080"])

    def tool(text: str) -> str:
        return text.upper()

    wrapped = proxy(tool, "tool", "desc")
    assert asyncio.run(wrapped("hi")) == "HI"
