"""The platform state store's HTTP surface, driven end to end over a booted stack —
the operator/API door a Studio States page and the ``tai states`` CLI travel.

One composed path on ``core_stack`` walks the whole ledger a real declaration takes:
declare a state with its subject kinds, upload a module and mount it (so a path is
governed by a ``composing`` regime), then drive the record doors — a whole-path
``set`` over the composing path is refused (422), the keyed ``set_by_key`` op is
accepted, the write ledger records the ``api`` door with the touched paths, a
content search finds the subject, a fold aliases one subject onto another, and an
additive migration re-validates every record. A second leg pins the OFF contract:
with no states database bound (``off_stack``), every door — a read and a write
alike — refuses ``501`` with the stable ``states-not-configured`` code rather than
serving an empty or forged answer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

# The OFF door's machine-readable refusal code (``operations.states._NOT_CONFIGURED_CODE``).
_NOT_CONFIGURED_CODE = "states-not-configured"


def _record_path(state: str, target_kind: str, target_name: str, kind: str, key: str) -> str:
    return f"/api/states/{state}/records/{target_kind}/{target_name}/{kind}/{key}"


async def test_core_stack_composed_state_store_path(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    state = uniq("status")
    module = uniq("box-mod").replace("_", "-")

    # -- declare a state with its subject kinds --------------------------------
    declaration = {
        "description": "e2e subject status",
        "schema": {
            "type": "object",
            "properties": {
                "note": {"type": "string"},
            },
        },
        "subject_kinds": ["thread"],
        "default_subject_kind": "thread",
    }
    saved = await api.put(f"/api/states/{state}", json=declaration)
    assert saved["name"] == state
    assert saved["default_subject_kind"] == "thread"

    served = await api.get(f"/api/states/{state}")
    assert served["subject_kinds"] == ["thread"]
    assert served["mounts"] == []

    # -- upload a module and mount it (a ``composing`` regime on box.items) -----
    await api.put(
        f"/api/state-modules/{module}",
        json={
            "kind": "state-module",
            "name": module,
            "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
            "regimes": [{"path": ["items"], "regime": "composing"}],
        },
    )
    await api.put(
        f"/api/states/{state}/mounts/{module}",
        json={"path": ["box"], "parameters": {}, "declarations": {}},
    )
    served = await api.get(f"/api/states/{state}")
    assert [m["module"] for m in served["mounts"]] == [module]
    assert {"path": ["box", "items"], "regime": "composing"} in served["regimes"]
    # The single read serves ``updated_at`` as a parseable ISO string through the real JSON
    # encoder (a raw datetime would 500 here) — the same field the list serves.
    datetime.fromisoformat(served["updated_at"].replace("Z", "+00:00"))

    # -- the list serves each declaration's ``updated_at`` (the Updated column) --
    listed = await api.get("/api/states")
    row = next(d for d in listed if d["name"] == state)
    datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))

    # -- the module catalog serves ``mounted_on`` + ``shipped_default`` ----------
    modules = await api.get("/api/state-modules")
    module_row = next(m for m in modules if m["name"] == module)
    assert module_row["mounted_on"] == 1
    # An operator-uploaded module is not a shipped default.
    assert module_row["shipped_default"] is False

    record = _record_path(state, "agent", "a-42", "thread", "t1")

    # -- a whole-path ``set`` over the composing path is refused (422) ----------
    resp = await api.request_raw(
        "POST", f"{record}/deltas", json={"ops": [{"op": "set", "path": ["box", "items"], "value": []}]}
    )
    assert resp.status_code == 422, resp.text

    # -- the keyed op is accepted ----------------------------------------------
    applied = await api.post(
        f"{record}/deltas",
        json={
            "ops": [
                {
                    "op": "set_by_key",
                    "path": ["box", "items"],
                    "key_field": "id",
                    "value": {"id": "a-42", "label": "status"},
                }
            ]
        },
    )
    assert applied["applied"] is True
    assert applied["data"]["box"]["items"] == [{"id": "a-42", "label": "status"}]

    read = await api.get(record)
    assert read["data"]["box"]["items"] == [{"id": "a-42", "label": "status"}]

    # -- the write ledger records the ``api`` door with the touched paths -------
    writes_page = await api.get(f"{record}/writes")
    # A single write fits one page, so the keyset cursor is exhausted (null).
    assert writes_page["next_cursor"] is None
    writes = writes_page["items"]
    assert len(writes) == 1
    assert writes[0]["origin"]["door"] == "api"
    assert writes[0]["paths"] == [["box", "items"]]

    # -- a content search finds the subject ------------------------------------
    found = await api.post(
        f"/api/states/{state}/records/search", json={"filters": {"box": {"items": [{"id": "a-42"}]}}}
    )
    matched = [m["subject"] for m in found["matches"]]
    assert {"target_kind": "agent", "target_name": "a-42", "kind": "thread", "key": "t1"} in matched

    # -- a fold aliases one subject onto another -------------------------------
    survivor = _record_path(state, "agent", "a-42", "thread", "t2")
    await api.put(survivor, json={"box": {"items": []}, "note": "survivor"})
    await api.post(
        f"{record}/fold",
        json={"into": {"target_kind": "agent", "target_name": "a-42", "kind": "thread", "key": "t2"}, "mode": "switch"},
    )
    folded = await api.get(record)
    assert folded["canonical_subject"]["key"] == "t2"

    # -- an additive migration re-validates every record -----------------------
    migrated = await api.post(
        f"/api/states/{state}/migrate",
        json={
            "new_schema": {
                "type": "object",
                "properties": {
                    "note": {"type": "string"},
                    "tier": {"type": "string"},
                },
            }
        },
    )
    assert migrated["migrated"] is True


async def _assert_states_off(off_stack: TaiStack, method: str, path: str, *, json=None) -> None:
    api: ApiClient = off_stack.api()
    resp: httpx.Response = await api.request_raw(method, path, json=json)
    assert resp.status_code == 501, f"{method} {path} -> {resp.status_code}; body: {resp.text}"
    body = resp.json()
    assert body.get("code") == _NOT_CONFIGURED_CODE, f"{method} {path} code={body.get('code')!r}; body: {resp.text}"
    assert isinstance(body.get("error"), str), resp.text
    assert body["error"], resp.text


async def test_off_stack_state_doors_refuse_501(off_stack: TaiStack) -> None:
    # A read door — no empty-degrade for the record store; it refuses loudly.
    await _assert_states_off(off_stack, "GET", "/api/states")
    await _assert_states_off(off_stack, "GET", "/api/state-modules")
    # A write door.
    await _assert_states_off(
        off_stack,
        "PUT",
        "/api/states/status-off",
        json={"schema": {"type": "object"}, "subject_kinds": ["thread"], "default_subject_kind": "thread"},
    )
    # A record write door.
    await _assert_states_off(
        off_stack,
        "POST",
        "/api/states/status-off/records/agent/a-42/thread/t1/deltas",
        json={"ops": [{"op": "set", "path": ["k"], "value": 1}]},
    )
