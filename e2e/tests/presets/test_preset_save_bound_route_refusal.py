"""A preset save or rollback is refused when the version it would activate cannot bind a conversation
route bound to the preset.

A route bound to a preset over ``tools_agent`` with no ``reply_expr`` / ``resume_expr`` binds
while the preset bakes ``tool_names`` without ``ask``. A version baking ``ask`` would leave the
route with no reply/resume path for a caller-ask, so a save of such a version, or a rollback to one,
is a 409 that names the write, the route, the preset it is bound to and the refusal line, and
changes nothing; a save that keeps the route bindable goes through. A route bound to a preset that
composes the saved one, and that could not bind before the save for a cause of its own, does not
refuse the save."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs(
    "kind:identity",
    "kind:versioning",
    "probe-tools",
    "setting:conversations",
    "setting:seeded-access-control",
    "setting:tools_agent",
    "setting:tool:ask",
)

# An https callback URL with no server behind it: no message is ever sent, the route only has to be valid.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


def _asking_line(preset: str) -> str:
    return (
        f"preset {preset!r} runs agent 'tools_agent', which can ask its caller (its tool_names include 'ask') "
        "but the route declares no reply_expr and no resume_expr: a caller-ask has no reply/resume path"
    )


async def _route_body(api: ApiClient, uniq: Callable[[str], str], preset: str) -> dict[str, str]:
    exec_key = uniq("bound-exec")
    await api.post("/api/auth/api-keys", json={"user_id": exec_key, "description": "e2e", "scopes": ["e2e-all"]})
    return {
        "door": "api",
        "target_kind": "tool",
        "target_name": preset,
        "execution_key": exec_key,
        "callback_url": _UNREACHABLE_CALLBACK,
    }


async def _bind_route(api: ApiClient, uniq: Callable[[str], str], preset: str) -> str:
    route = uniq("bound-route").replace("_", "-")
    await api.post(f"/api/conversations/{route}", json=await _route_body(api, uniq, preset))
    return route


async def test_a_save_that_cannot_bind_a_bound_route_is_refused_naming_it(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, _root_token = bridge_stack
    api = stack.api()
    preset = uniq("bound")
    await api.post(
        "/api/presets",
        json={
            "name": preset,
            "base_tool": "tools_agent",
            "description": "agent preset bound to a route",
            "fixed_kwargs": {"tool_names": ["e2e_echo"]},
        },
    )
    route = await _bind_route(api, uniq, preset)

    refused = await api.request_raw(
        "POST", f"/api/presets/{preset}/versions", json={"fixed_kwargs": {"tool_names": ["ask"]}}
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == (
        f"preset {preset!r} cannot be saved — the new version would leave conversation route(s) unable to bind: "
        f"route {route!r}, bound to preset {preset!r}: the new version cannot bind it "
        f"({_asking_line(preset)})"
    )
    assert (await api.get(f"/api/presets/{preset}"))["fixed_kwargs"] == {"tool_names": ["e2e_echo"]}

    await api.post(f"/api/presets/{preset}/versions", json={"fixed_kwargs": {"tool_names": []}})
    assert (await api.get(f"/api/presets/{preset}"))["fixed_kwargs"] == {"tool_names": []}

    await api.delete(f"/api/conversations/{route}")
    await api.delete(f"/api/presets/{preset}")


@pytest.mark.needs("setting:router:backup")
async def test_a_save_of_a_preset_whose_composer_route_already_could_not_bind_goes_through(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, _root_token = bridge_stack
    api = stack.api()
    composed = uniq("composed")
    await api.post(
        "/api/presets",
        json={
            "name": composed,
            "base_tool": "tools_agent",
            "description": "agent preset another preset composes",
            "fixed_kwargs": {"tool_names": ["e2e_echo"]},
        },
    )
    composer = uniq("composer")
    await api.post(
        "/api/presets",
        json={
            "name": composer,
            "base_tool": "tools_agent",
            "description": "agent preset composing another preset",
            "fixed_kwargs": {"tool_names": ["ask", composed]},
        },
    )
    # A backup holding the composer's asking version, cut down to the composer's own document.
    export = await api.request_raw("POST", "/api/backup/export", json={"sections": ["versioned_documents"]})
    assert export.status_code == 200, export.text
    document = export.json()
    section = document["sections"]["versioned_documents"]
    (composer_row,) = [row for row in section["documents"] if row["kind"] == "preset" and row["name"] == composer]
    section["documents"] = [composer_row]
    section["versions"] = [row for row in section["versions"] if row["document_id"] == composer_row["id"]]

    await api.post(f"/api/presets/{composer}/versions", json={"fixed_kwargs": {"tool_names": [composed]}})
    route = await _bind_route(api, uniq, composer)

    # The restore re-activates the asking version: the composer's route can no longer bind, for a cause
    # (its own ``ask``) the composed preset has no part in.
    restored = await api.post(
        "/api/backup/import", json={"document": document, "sections": ["versioned_documents"], "mode": "overwrite"}
    )
    assert restored["ok"], restored
    assert (await api.get(f"/api/presets/{composer}"))["fixed_kwargs"] == {"tool_names": ["ask", composed]}
    rebind = await api.request_raw("POST", f"/api/conversations/{route}", json=await _route_body(api, uniq, composer))
    assert rebind.status_code == 422, rebind.text
    assert _asking_line(composer) in rebind.json()["error"]

    # The composer composes the saved preset, so the save judges the composer's route too.
    assert (await api.get(f"/api/presets/{composed}"))["used_by"] == [composer]
    saved = await api.post(f"/api/presets/{composed}/versions", json={"fixed_kwargs": {"tool_names": []}})
    assert saved["version"] == 2
    assert (await api.get(f"/api/presets/{composed}"))["fixed_kwargs"] == {"tool_names": []}

    await api.delete(f"/api/conversations/{route}")
    await api.delete(f"/api/presets/{composer}")
    await api.delete(f"/api/presets/{composed}")


async def test_a_rollback_to_a_version_that_cannot_bind_a_bound_route_is_refused_naming_it(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, _root_token = bridge_stack
    api = stack.api()
    preset = uniq("bound")
    created = await api.post(
        "/api/presets",
        json={
            "name": preset,
            "base_tool": "tools_agent",
            "description": "agent preset bound to a route",
            "fixed_kwargs": {"tool_names": ["ask"]},
        },
    )
    asking_version = created["active_version"]
    saved = await api.post(f"/api/presets/{preset}/versions", json={"fixed_kwargs": {"tool_names": []}})
    quiet_version = saved["version"]
    route = await _bind_route(api, uniq, preset)

    refused = await api.request_raw("POST", f"/api/presets/{preset}/rollback", json={"version": asking_version})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == (
        f"preset {preset!r} cannot be rolled back to version {asking_version} — that version would leave "
        f"conversation route(s) unable to bind: route {route!r}, bound to preset {preset!r}: "
        f"version {asking_version} cannot bind it ({_asking_line(preset)})"
    )
    record = await api.get(f"/api/presets/{preset}")
    assert record["active_version"] == quiet_version
    assert record["fixed_kwargs"] == {"tool_names": []}

    await api.delete(f"/api/conversations/{route}")
    await api.delete(f"/api/presets/{preset}")
