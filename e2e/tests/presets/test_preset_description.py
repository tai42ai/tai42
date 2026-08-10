"""C7 — the preset ``description`` contract over the real versioning store: the
required-non-empty create door, and the per-version editable description
(None-carry-forward on save, restored on rollback).

``description`` is the bound tool's LLM-facing docstring — a REQUIRED non-empty
field on every create path, and a versioned, editable ``save_version`` field:
an omitted value carries the active text forward, an explicit string sets
it, and a rollback restores the older version's text. It is never conflated with
the tool_meta ``display_name`` organizational overlay."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack


async def _create(api: ApiClient, name: str, description: str) -> dict:
    return await api.post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "e2e_echo",
            "description": description,
            "fixed_kwargs": {"payload": "baked"},
        },
    )


async def test_create_without_description_is_400(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    # A missing description is a loud 400 at the create door (the required-field edge).
    missing = await api.request_raw(
        "POST",
        "/api/presets",
        json={"name": uniq("preset"), "base_tool": "e2e_echo", "fixed_kwargs": {"payload": "baked"}},
    )
    assert missing.status_code == 400, missing.text

    # An explicit empty/whitespace description is rejected just as loudly.
    blank = await api.request_raw(
        "POST",
        "/api/presets",
        json={"name": uniq("preset"), "base_tool": "e2e_echo", "description": "   ", "fixed_kwargs": {}},
    )
    assert blank.status_code == 400, blank.text


async def test_save_version_edits_description_and_carries_forward(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = core_stack.api()
    name = uniq("preset")
    await _create(api, name, "original description")
    assert (await api.get(f"/api/presets/{name}"))["description"] == "original description"

    # An explicit description on a save-version SETS the active text.
    await api.post(f"/api/presets/{name}/versions", json={"description": "second description"})
    assert (await api.get(f"/api/presets/{name}"))["description"] == "second description"

    # A save-version that OMITS description carries the active text forward.
    await api.post(f"/api/presets/{name}/versions", json={"fixed_kwargs": {"payload": "again"}})
    assert (await api.get(f"/api/presets/{name}"))["description"] == "second description"

    # An explicit empty description on a save-version is rejected (never a blank docstring).
    blank = await api.request_raw("POST", f"/api/presets/{name}/versions", json={"description": ""})
    assert blank.status_code == 400, blank.text


async def test_rollback_restores_older_description(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    name = uniq("preset")
    created = await _create(api, name, "v1 description")
    v1 = created["active_version"]

    await api.post(f"/api/presets/{name}/versions", json={"description": "v2 description"})
    assert (await api.get(f"/api/presets/{name}"))["description"] == "v2 description"

    # Rolling back to v1 restores its older description text.
    await api.post(f"/api/presets/{name}/rollback", json={"version": v1})
    assert (await api.get(f"/api/presets/{name}"))["description"] == "v1 description"
