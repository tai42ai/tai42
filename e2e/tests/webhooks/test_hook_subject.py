"""The hook door of the platform state store — a webhook fire whose hook declares a
``subject`` writes the subject's record under the platform-stamped ``hook`` provenance,
and a keyed write under a TRACED mount is stamped with ``_trace`` by the platform.

Over ``replicas_stack`` (the webhook home, backend ON, access control OFF): a hook binds
``state_apply`` with a ``subject`` whose ``key_expr`` reads the event payload.

- ``test_hook_subject_keys_record_and_ledgers_the_hook_door`` — a delivery carrying the
  key keys the subject, the tool's keyed op lands the record under the declared kind, and
  the write ledger records the ``hook`` door with the actor set to the hook's execution key
  and a null ``turn_id`` (a fire is not a conversation turn). A delivery whose payload lacks
  the key fails the fire loudly and writes no record.
- ``test_hook_keyed_write_under_a_traced_mount_stamps_trace`` — the same fire, but the op
  writes under a mount whose module traces, so each written item carries the platform's
  ``_trace`` stamp (meta / run / at), the same stamp any consumer's write gets (D-3: the platform
  stamps ``_trace`` under a traced mount for EVERY door, not only one consumer's).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

_TARGET_KIND = "agent"
_TARGET_NAME = "a-42"
_SUBJECT_KIND = "watch"
_SUBJECT_KEY = "w1"


def _record_path(state: str, key: str) -> str:
    return f"/api/states/{state}/records/{_TARGET_KIND}/{_TARGET_NAME}/{_SUBJECT_KIND}/{key}"


async def _declare_state(api: ApiClient, name: str) -> None:
    await api.put(
        f"/api/states/{name}",
        json={
            "description": "e2e hook-keyed watch status",
            "schema": {"type": "object", "properties": {"note": {"type": "string"}}},
            "subject_kinds": [_SUBJECT_KIND],
            "default_subject_kind": _SUBJECT_KIND,
        },
    )


async def _register_hook(api: ApiClient, *, topic: str, state: str, path: list[str], execution_key: str) -> None:
    await api.post(
        "/api/hooks",
        json={
            "name": f"{topic}-hook",
            "topic": topic,
            "tool": "state_apply",
            "tool_kwargs": {
                "state": state,
                "ops": [{"op": "set_by_key", "path": path, "key_field": "id", "value": {"id": _SUBJECT_KEY}}],
            },
            "execution_key": execution_key,
            "subject": {
                "target_kind": _TARGET_KIND,
                "target_name": _TARGET_NAME,
                "kind": _SUBJECT_KIND,
                "key_expr": ".key",
            },
        },
    )


async def _wait_record(api: ApiClient, state: str, key: str) -> dict[str, Any]:
    async def probe() -> dict[str, Any] | None:
        return await api.get(_record_path(state, key))

    return await wait_for_async(
        probe, deadline=20.0, message=f"the hook fire never wrote the {key!r} record for {state!r}"
    )


async def test_hook_subject_keys_record_and_ledgers_the_hook_door(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)
    state = uniq("watchstatus")
    # The base schema carries the keyed array itself (no mount) so the door + subject
    # provenance is exercised on a plain keyed write.
    await api.put(
        f"/api/states/{state}",
        json={
            "description": "e2e hook-keyed watch status",
            "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
            "subject_kinds": [_SUBJECT_KIND],
            "default_subject_kind": _SUBJECT_KIND,
        },
    )
    topic = uniq("watch-topic").replace("_", "-")
    execution_key = uniq("watch-exec")
    await _register_hook(api, topic=topic, state=state, path=["items"], execution_key=execution_key)

    # A delivery WITHOUT the key: the subject cannot be keyed, so the fire fails loudly and
    # writes nothing — fired first so the later one-subject assertion also proves it.
    await api.request_raw("POST", f"/universal_webhook/{topic}", json={"nokey": "payload"})
    # A delivery carrying the key: the fire keys the subject and the tool writes the record.
    await api.request_raw("POST", f"/universal_webhook/{topic}", json={"key": _SUBJECT_KEY, "label": "status"})

    record = await _wait_record(api, state, _SUBJECT_KEY)
    assert record["data"]["items"][0]["id"] == _SUBJECT_KEY
    # The record is keyed under the declared kind and the exact key the subject resolved.
    assert record["subject"]["kind"] == _SUBJECT_KIND
    assert record["subject"]["key"] == _SUBJECT_KEY

    writes = (await api.get(f"{_record_path(state, _SUBJECT_KEY)}/writes"))["items"]
    assert writes, "the hook fire wrote no ledger row"
    origin = writes[0]["origin"]
    assert origin["door"] == "hook"
    assert origin["actor"] == execution_key
    assert origin["turn_id"] is None, origin

    # The failed (keyless) fire created no record: the state holds exactly the one subject.
    subjects = await api.get(f"/api/states/{state}/subjects")
    assert [s["subject"]["key"] for s in subjects["subjects"]] == [_SUBJECT_KEY], subjects


async def test_hook_keyed_write_under_a_traced_mount_stamps_trace(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)
    state = uniq("tracedwatch")
    module = uniq("trace-mod").replace("_", "-")
    await _declare_state(api, state)
    # A traced module lands ``a.items`` (each item admits ``_trace``); a write under the
    # mount is ``_trace``-stamped by the platform for EVERY door (D-3).
    await api.put(
        f"/api/state-modules/{module}",
        json={
            "kind": "state-module",
            "name": module,
            "schema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"id": {"type": "string"}}},
                    }
                },
            },
            "trace": {"enabled": True},
        },
    )
    await api.put(f"/api/states/{state}/mounts/{module}", json={"path": ["a"], "parameters": {}, "declarations": {}})

    topic = uniq("traced-topic").replace("_", "-")
    execution_key = uniq("traced-exec")
    await _register_hook(api, topic=topic, state=state, path=["a", "items"], execution_key=execution_key)
    await api.request_raw("POST", f"/universal_webhook/{topic}", json={"key": _SUBJECT_KEY, "label": "status"})

    record = await _wait_record(api, state, _SUBJECT_KEY)
    item = record["data"]["a"]["items"][0]
    assert item["id"] == _SUBJECT_KEY
    # The platform stamped ``_trace`` under the traced mount — meta / run / at present.
    trace = item["_trace"]
    assert {"meta", "run", "at"} <= set(trace), trace
    assert trace["at"], trace

    writes = (await api.get(f"{_record_path(state, _SUBJECT_KEY)}/writes"))["items"]
    assert writes[0]["origin"]["door"] == "hook"
    assert writes[0]["origin"]["actor"] == execution_key
