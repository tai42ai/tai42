"""Stored-template ids at the definition doors and at the delete doors.

A definition naming a stored id the store lacks is refused with the site it names, and
stores nothing; once the id is uploaded the same definition is accepted. A stored object
a hook, a preset or a setting names cannot be deleted through either delete door, file or
directory, until that holder lets go.

Over ``bridge_stack``: access control ON with the seeded root, the presets, hooks,
schedules, conversations, api-keys, config, storage and templates doors, and the
``tools_agent`` agent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs(
    "kind:identity",
    "kind:storage",
    "probe-tools",
    "setting:seeded-access-control",
    "setting:seeded-key-id=bridge-root",
)

# The seeded root key's id: the root may bind it as any hook's, route's or schedule's identity.
_ROOT_KEY_ID = "bridge-root"
_SUMMARY_PROMPT_ENV = "SUMMARIZATION_MIDDLEWARE_SUMMARY_PROMPT"
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


def _root_api(bridge_stack: tuple[TaiStack, str]) -> ApiClient:
    stack, root_token = bridge_stack
    return stack.api(stack.port_a).with_token(root_token)


def _missing(template_id: str, site: str) -> str:
    return f"stored id {template_id!r} at {site} is not a stored template"


async def _upload(api: ApiClient, template_id: str, content: str) -> None:
    await api.post("/api/storage/resources", json={"id": template_id, "content_text": content})


async def _refused(api: ApiClient, method: str, path: str, body: dict[str, Any]) -> str:
    response = await api.request_raw(method, path, json=body)
    assert response.status_code == 400, response.text
    return response.json()["error"]


async def _preset_names(api: ApiClient) -> list[str]:
    return [row["name"] for row in await api.get("/api/presets", retry_on_reloading=True)]


def _hook(name: str, tool: str, uniq: Callable[[str], str], **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "topic": uniq("events").replace("_", "-"),
        "tool": tool,
        "execution_key": _ROOT_KEY_ID,
        **fields,
    }


def _held_refusal(subject: str, *lines: str) -> str:
    held = "".join(f"- {line}\n" for line in lines)
    return f"{subject} cannot be deleted — held by:\n{held}remove those references first"


async def _assert_held(api: ApiClient, template_id: str, kind: str, line: str) -> None:
    """Both file deletes and both directory deletes answer 409 naming the one holder; the object stays."""
    directory = template_id.rsplit("/", 1)[0]
    file_refusal = _held_refusal(f"stored template {template_id!r}", line)
    dir_refusal = _held_refusal(f"directory {directory!r}", line)
    for method, path, body, refusal in (
        ("DELETE", f"/api/storage/resources/{template_id}", None, file_refusal),
        ("POST", "/api/delete-template", {"path": template_id}, file_refusal),
        ("DELETE", f"/api/storage/dirs/{directory}", None, dir_refusal),
        ("POST", "/api/delete-template-dir", {"path": directory}, dir_refusal),
    ):
        response = await api.request_raw(method, path, json=body)
        assert response.status_code == 409, f"{method} {path}: {response.status_code} {response.text}"
        answer = response.json()
        assert answer["error"] == refusal
        assert [(holder["kind"], holder["summary"]) for holder in answer["holders"]] == [(kind, line)]
    entry = await api.get(f"/api/storage/entries/{template_id}")
    assert entry["kind"] == "text"


async def test_a_preset_naming_a_missing_output_schema_is_refused_until_it_is_stored(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    api = _root_api(bridge_stack)
    schema_id = f"{uniq('schemas')}/out"
    name = uniq("schema-preset")
    body = {"name": name, "base_tool": "e2e_echo", "description": "echo", "output_schema": {"id": schema_id}}
    assert await _refused(api, "POST", "/api/presets", body) == _missing(schema_id, "output_schema")
    assert name not in await _preset_names(api)
    await _upload(api, schema_id, '{"type": "object"}')
    try:
        await api.post("/api/presets", json=body, retry_on_reloading=True)
        assert name in await _preset_names(api)
        await api.delete(f"/api/presets/{name}", retry_on_reloading=True)
    finally:
        await api.delete(f"/api/storage/resources/{schema_id}")


async def test_an_agent_prompt_id_is_read_at_the_preset_and_hook_doors(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    api = _root_api(bridge_stack)
    prompt_id = f"{uniq('prompts')}/missing"
    name = uniq("agent-preset")
    prompt = {"system_prompt": {"id": prompt_id}}
    body = {"name": name, "base_tool": "tools_agent", "description": "agent", "fixed_kwargs": prompt}
    assert await _refused(api, "POST", "/api/presets", body) == _missing(prompt_id, "system_prompt")
    hook = _hook(uniq("agent-hook"), "tools_agent", uniq, tool_kwargs=prompt)
    assert await _refused(api, "POST", "/api/hooks", hook) == _missing(prompt_id, "tool_kwargs > system_prompt")
    # A locale variant alone does not satisfy the bare id.
    await _upload(api, f"{prompt_id}@he", "shalom")
    try:
        assert await _refused(api, "POST", "/api/presets", body) == _missing(prompt_id, "system_prompt")
        assert name not in await _preset_names(api)
        await _upload(api, prompt_id, "You answer briefly.")
        await api.post("/api/presets", json=body, retry_on_reloading=True)
        assert name in await _preset_names(api)
        await api.delete(f"/api/presets/{name}", retry_on_reloading=True)
    finally:
        await api.delete(f"/api/storage/resources/{prompt_id}@he")
        await api.delete(f"/api/storage/resources/{prompt_id}")


@pytest.mark.needs("kind:backend", "setting:conversations:redis")
async def test_every_other_definition_door_reads_its_stored_ids(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    api = _root_api(bridge_stack)
    jq_id = f"{uniq('jq')}/gate"
    hook = _hook(uniq("gate-hook"), "e2e_echo", uniq, condition={"id": jq_id})
    route_name = uniq("ref-route").replace("_", "-")
    route = {
        "door": "api",
        "target_kind": "tool",
        "target_name": "e2e_echo",
        "execution_key": _ROOT_KEY_ID,
        "callback_url": _UNREACHABLE_CALLBACK,
        "reply_expr": {"id": jq_id},
    }
    schedule = {
        "tool_name": "e2e_echo",
        "tool_kwargs": {"payload": "once"},
        "execution_key": _ROOT_KEY_ID,
        "start_expr": {"id": jq_id},
    }
    key_user = uniq("ref-key")
    key = {"user_id": key_user, "description": "condition key", "scopes": ["e2e-all"], "condition": {"id": jq_id}}

    refusals = [
        await _refused(api, "POST", "/api/hooks", hook),
        await _refused(api, "POST", f"/api/conversations/{route_name}", route),
        await _refused(api, "POST", "/api/schedules", schedule),
        await _refused(api, "POST", "/api/auth/api-keys", key),
    ]
    assert refusals == [
        f"invalid hook params: {_missing(jq_id, 'condition')}",
        f"invalid conversation route: {_missing(jq_id, 'reply_expr')}",
        f"invalid schedule: {_missing(jq_id, 'start_expr')}",
        _missing(jq_id, "condition"),
    ]

    await _upload(api, jq_id, ".")
    await api.post("/api/hooks", json=hook, retry_on_reloading=True)
    await api.post(f"/api/conversations/{route_name}", json=route)
    await api.post("/api/schedules", json=schedule, retry_on_reloading=True)
    await api.post("/api/auth/api-keys", json=key)

    await api.delete(f"/api/hooks/{hook['name']}")
    await api.delete(f"/api/conversations/{route_name}")
    await api.delete(f"/api/auth/api-keys/{key_user}")
    await api.delete(f"/api/storage/resources/{jq_id}")


async def test_a_template_a_hook_names_is_held_until_the_hook_is_deleted(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    api = _root_api(bridge_stack)
    jq_id = f"{uniq('jq')}/gate"
    hook_name = uniq("holder-hook")
    await _upload(api, jq_id, ".ok")
    await api.post("/api/hooks", json=_hook(hook_name, "e2e_echo", uniq, condition={"id": jq_id}))
    try:
        await _assert_held(api, jq_id, "hook", f"hook {hook_name!r} names {jq_id!r} at condition")
    finally:
        await api.delete(f"/api/hooks/{hook_name}")
    await api.delete(f"/api/storage/resources/{jq_id}")


async def test_a_template_a_preset_prompt_names_is_held_until_the_preset_is_deleted(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    api = _root_api(bridge_stack)
    prompt_id = f"{uniq('prompts')}/system"
    name = uniq("holder-preset")
    await _upload(api, prompt_id, "You answer briefly.")
    body = {
        "name": name,
        "base_tool": "tools_agent",
        "description": "agent",
        "fixed_kwargs": {"system_prompt": {"id": prompt_id}},
    }
    await api.post("/api/presets", json=body, retry_on_reloading=True)
    try:
        await _assert_held(api, prompt_id, "preset", f"preset {name!r} names {prompt_id!r} at system_prompt")
    finally:
        await api.delete(f"/api/presets/{name}", retry_on_reloading=True)
    await api.delete(f"/api/storage/resources/{prompt_id}")


@pytest.mark.needs("mutable")
async def test_a_template_the_summary_prompt_setting_names_is_held(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    api = _root_api(bridge_stack)
    prompt_id = f"{uniq('prompts')}/summary"
    await _upload(api, prompt_id, "Summarize the conversation.")
    setting = json.dumps({"id": prompt_id})
    await api.post("/api/config/env", json={"env": {_SUMMARY_PROMPT_ENV: setting}}, retry_on_reloading=True)
    try:
        line = f"setting {_SUMMARY_PROMPT_ENV!r} names {prompt_id!r} at summary_prompt"
        response = await api.request_raw("DELETE", f"/api/storage/resources/{prompt_id}")
        assert response.status_code == 409, response.text
        assert response.json()["error"] == _held_refusal(f"stored template {prompt_id!r}", line)
    finally:
        inline = json.dumps({"content": "Summarize the conversation."})
        await api.post("/api/config/env", json={"env": {_SUMMARY_PROMPT_ENV: inline}}, retry_on_reloading=True)
    await api.delete(f"/api/storage/resources/{prompt_id}", retry_on_reloading=True)
