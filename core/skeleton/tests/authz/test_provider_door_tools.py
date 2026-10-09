"""The provider door as MCP tools: which of its operations the projection serves, and the administrator
fence on the tool edge, through the real ``AuthzMiddleware`` over the connectors router.

The look-up is projected like the catalog reads, adding and removing a provider like the destructive
connect operations (withheld when ``expose_destructive`` is off), and setting a client's credentials is
authority-changing (off the default surface, projected only when included). Each projected door operation is
refused to a non-administrator before its body runs, as its route is.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations.errors import PermissionDeniedError

from .test_middleware_fence import _bind_caller, _Dispatcher, ac  # noqa: F401  (ac: the policy-store fixture)

pytestmark = pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning")

_DOOR = ("discover_connector_server", "add_connector_provider", "remove_connector_provider", "set_connector_client")


def _manifest(**api_tools: Any) -> Manifest:
    return Manifest.model_validate(
        {
            "api_tools": {"enabled": True, **api_tools},
            "routers_modules": ["tai42_skeleton.routers.connectors"],
            "default_routers": "none",
        }
    )


def _projected(manifest: Manifest) -> set[str]:
    async def run() -> set[str]:
        async with app.app_context(manifest):
            return set(await app.tools.get_tools())

    return asyncio.run(run()) & set(_DOOR)


def test_by_default_set_client_stays_off_while_the_other_three_are_projected(ac) -> None:  # noqa: F811
    assert _projected(_manifest()) == {
        "discover_connector_server",
        "add_connector_provider",
        "remove_connector_provider",
    }


def test_with_destructive_tools_withheld_only_the_look_up_is_projected(ac) -> None:  # noqa: F811
    assert _projected(_manifest(expose_destructive=False)) == {"discover_connector_server"}


def test_set_client_is_projected_only_when_included(ac) -> None:  # noqa: F811
    assert "set_connector_client" in _projected(_manifest(include=["set_connector_client"]))


async def _dispatch(user_id: str, tool: str, arguments: dict[str, Any]) -> str:
    reset = _bind_caller(user_id, ("*",))
    try:
        return await _Dispatcher().dispatch(tool, arguments)
    finally:
        reset()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("discover_connector_server", {"url": "https://mcp.example.com/mcp"}),
        ("add_connector_provider", {"url": "https://mcp.example.com/mcp"}),
        ("remove_connector_provider", {"provider_id": "remote"}),
        ("set_connector_client", {"provider_id": "remote", "client_id": "app"}),
    ],
)
def test_a_non_administrator_is_refused_each_door_tool_before_it_runs(ac, tool: str, arguments: dict) -> None:  # noqa: F811
    manifest = _manifest(include=["set_connector_client"])

    async def run() -> tuple[ToolError, str]:
        async with app.app_context(manifest):
            dispatcher = _Dispatcher()
            reset = _bind_caller("editor-key", ("*",))
            try:
                with pytest.raises(ToolError) as denied:
                    await dispatcher.dispatch(tool, arguments)
            finally:
                reset()
            assert dispatcher.reached == 0
            return denied.value, await _dispatch("root", tool, arguments)

    denied, admitted = asyncio.run(run())
    assert isinstance(denied.__cause__, PermissionDeniedError)
    assert "is not permitted for 'editor-key'" in str(denied)
    assert admitted == "reached"
