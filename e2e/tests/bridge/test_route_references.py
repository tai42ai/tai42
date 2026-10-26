"""A conversation route's target is a reference the live registry must serve, and a bound
route holds the preset it targets.

A route create naming a tool no registry serves is a 400 that names it and stores no route.
A route bound to a preset is listed by the preset's delete preview, and the preset delete is
refused naming the same route until the route is gone."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ._bridge_support import BridgeHarness

pytestmark = pytest.mark.needs(
    "kind:identity", "kind:versioning", "probe-tools", "setting:conversations", "setting:seeded-access-control"
)

# An https callback URL with no server behind it: these legs never send a message, so the
# route only has to be valid.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


async def test_route_to_an_unserved_tool_is_refused(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    exec_key = uniq("ref-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route = uniq("ref-ghost-route").replace("_", "-")
    ghost = uniq("ghost")

    refused = await bridge.api().request_raw(
        "POST",
        f"/api/conversations/{route}",
        json={
            "door": "api",
            "target_kind": "tool",
            "target_name": ghost,
            "execution_key": exec_key,
            "callback_url": _UNREACHABLE_CALLBACK,
        },
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"] == f"route target tool {ghost!r} is not a registered tool"
    await bridge.api().get(f"/api/conversations/{route}", expect=404)


async def test_delete_preview_lists_a_bound_route(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    api = bridge.api()
    preset = uniq("routed")
    await api.post(
        "/api/presets",
        json={
            "name": preset,
            "base_tool": "e2e_echo",
            "description": "route-bound echo preset",
            "fixed_kwargs": {"payload": "bound"},
        },
        retry_on_reloading=True,
    )
    assert await api.get(f"/api/presets/{preset}/referees?action=delete") == {"action": "delete", "holders": []}

    exec_key = uniq("ref-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route = uniq("ref-bound-route").replace("_", "-")
    await bridge.create_tool_api_route(
        route_name=route, tool=preset, execution_key=exec_key, callback_url=_UNREACHABLE_CALLBACK
    )

    holder = {
        "kind": "conversation route",
        "tag": "conversations",
        "name": route,
        "locator": {},
        "summary": f"conversation route {route!r}",
    }
    assert await api.get(f"/api/presets/{preset}/referees?action=delete") == {"action": "delete", "holders": [holder]}

    refused = await api.request_raw("DELETE", f"/api/presets/{preset}")
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == (
        f"preset {preset!r} cannot be deleted — held by:\n- conversation route {route!r}\nremove those references first"
    )
    assert refused.json()["holders"] == [holder]

    await api.delete(f"/api/conversations/{route}")
    await api.delete(f"/api/presets/{preset}")
