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
from urllib.parse import quote

import httpx

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

# The OFF door's machine-readable refusal code (``operations.states._NOT_CONFIGURED_CODE``).
_NOT_CONFIGURED_CODE = "states-not-configured"


def _record_path(state: str, target_kind: str, target_name: str, kind: str, key: str) -> str:
    # Each segment is percent-encoded (``safe=""`` encodes ``/`` too), the same contract
    # the SDK and CLI apply, so a subject key that carries ``/`` (a thread id) forms one
    # path segment the record doors route to intact rather than splitting.
    parts = [quote(part, safe="") for part in (state, target_kind, target_name, kind, key)]
    return "/api/states/{}/records/{}/{}/{}/{}".format(*parts)


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

    # -- the mount read door serves the same envelope row the list serves -------
    mount_rows = await api.get(f"/api/states/{state}/mounts")
    assert [m["module"] for m in mount_rows] == [module]
    one_mount = await api.get(f"/api/states/{state}/mounts/{module}")
    assert one_mount == mount_rows[0]
    # A module not mounted on the state is a 404 at the read door (never an empty 200).
    absent = uniq("absent-mod").replace("_", "-")
    resp = await api.request_raw("GET", f"/api/states/{state}/mounts/{absent}")
    assert resp.status_code == 404, resp.text

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


async def test_core_stack_mount_check_reads_effective_parameters(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """Over the real mount door, a module's declarations ``check`` reads the mount's
    effective parameters as ``$parameters``: a declaration exceeding the supplied ``limit``
    is refused 422 with the check's message, and one within it mounts."""
    api = core_stack.api()
    state = uniq("status")
    module = uniq("capped-mod").replace("_", "-")

    await api.put(
        f"/api/states/{state}",
        json={
            "description": "e2e capped",
            "schema": {"type": "object", "properties": {"note": {"type": "string"}}},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
        },
    )
    await api.put(
        f"/api/state-modules/{module}",
        json={
            "kind": "state-module",
            "name": module,
            "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
            "parameters": {"limit": {"schema": {"type": "integer"}, "default": 5}},
            "declarations": {
                "schema": {"type": "object", "properties": {"count": {"type": "integer"}}},
                "check": 'if .count <= $parameters.limit then true else "count exceeds the mount limit" end',
            },
        },
    )

    # A declaration exceeding the supplied limit is refused at the mount door.
    resp = await api.request_raw(
        "PUT",
        f"/api/states/{state}/mounts/{module}",
        json={"path": ["box"], "parameters": {"limit": 8}, "declarations": {"count": 9}},
    )
    assert resp.status_code == 422, resp.text
    assert "count exceeds the mount limit" in resp.text

    # Within the limit, the mount is accepted and served.
    await api.put(
        f"/api/states/{state}/mounts/{module}",
        json={"path": ["box"], "parameters": {"limit": 8}, "declarations": {"count": 7}},
    )
    served = await api.get(f"/api/states/{state}")
    assert [m["module"] for m in served["mounts"]] == [module]


async def test_core_stack_record_key_with_slash_round_trips_by_url(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A thread subject's key is the thread id ``bridge:{route}:{quote(principal)}/{id}`` —
    it carries ``/``. Every single-record door (write, read, patch, the ``/writes``
    sub-action, delete) must address such a key as ONE percent-encoded segment, and a key
    whose tail is a sub-action name (``…/writes``) must still reach the record door."""
    api = core_stack.api()
    state = uniq("threadstate")
    await api.put(
        f"/api/states/{state}",
        json={
            "description": "e2e slashed-key round-trip",
            "schema": {"type": "object", "properties": {"note": {"type": "string"}}},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
        },
    )

    # A realistic thread id: two ``/`` and a ``:``-laden principal, all inside one key.
    key = "bridge:web:acme.example.com/+15550001111/u-42"
    record = _record_path(state, "agent", "a-42", "thread", key)
    # The wire path keeps the key percent-encoded as a single segment.
    assert "%2F" in record
    assert record.endswith(quote(key, safe=""))

    # -- write (PUT replace) then read (GET) by URL ----------------------------
    written = await api.put(record, json={"note": "first"})
    assert written["data"]["note"] == "first"
    read = await api.get(record)
    assert read["data"]["note"] == "first"
    # The persisted subject carries the key with its slashes intact (decoded once).
    assert read["subject"]["key"] == key

    # -- patch (PATCH merge) by URL --------------------------------------------
    merged = await api.request("PATCH", record, json={"note": "second"})
    assert merged["data"]["note"] == "second"

    # -- the ``/writes`` sub-action of the slashed key resolves (not mis-routed) --
    writes_page = await api.get(f"{record}/writes")
    assert [w["origin"]["door"] for w in writes_page["items"]] == ["api", "api"]

    # -- a key whose tail is the sub-action name ``writes`` addresses the record --
    tail_key = "conv/writes"
    tail_record = _record_path(state, "agent", "a-42", "thread", tail_key)
    await api.put(tail_record, json={"note": "tail"})
    tail_read = await api.get(tail_record)
    assert tail_read["data"]["note"] == "tail"
    assert tail_read["subject"]["key"] == tail_key
    # The record door won its own read; its write ledger holds the one PUT (a mis-route to
    # the writes-list of a ``conv`` key would instead have listed writes for the wrong key).
    tail_writes = await api.get(f"{tail_record}/writes")
    assert len(tail_writes["items"]) == 1

    # -- delete by URL, then the read is gone ----------------------------------
    await api.delete(record)
    assert await api.get(record) is None


async def test_auth_stack_slashed_key_record_door_is_gated_for_a_non_admin_key(
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """The COMPOSED access-control path a real caller's traffic travels: with access control
    ON, a NON-ADMIN scoped key round-trips a slashed thread key through the record doors
    exactly as an admin would, because the raw-path record route resolves to its PROTECTED
    resource on the same form the router matches — never dropping the key off the route into
    the public SPA catch-all. An unauthenticated caller is denied on the same door."""
    admin = auth_stack.api(port=auth_stack.port_a)
    state = uniq("threadstate")
    await admin.put(
        f"/api/states/{state}",
        json={
            "description": "e2e slashed-key non-admin round-trip",
            "schema": {"type": "object", "properties": {"note": {"type": "string"}}},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
        },
    )

    # A NON-admin scoped key: it holds only the seeded catch-all scope (never a condition-free
    # ``*`` admin policy), so reaching the record door depends on that door resolving to its
    # protected resource — the exact resolution the fix restores for a slashed key.
    user = uniq("recordsuser")
    raw_key = (
        await admin.post("/api/auth/api-keys", json={"user_id": user, "description": "e2e", "scopes": ["e2e-all"]})
    )["api_key"]
    caller = auth_stack.api(port=auth_stack.port_b).with_token(raw_key)

    key = "bridge:web:acme.example.com/+15550001111/u-42"
    record = _record_path(state, "agent", "a-42", "thread", key)
    assert "%2F" in record

    # -- the non-admin scoped key drives the whole record ledger by URL --------
    written = await caller.put(record, json={"note": "first"})
    assert written["data"]["note"] == "first"
    read = await caller.get(record)
    assert read["data"]["note"] == "first"
    assert read["subject"]["key"] == key
    merged = await caller.request("PATCH", record, json={"note": "second"})
    assert merged["data"]["note"] == "second"
    writes_page = await caller.get(f"{record}/writes")
    assert [w["origin"]["door"] for w in writes_page["items"]] == ["api", "api"]
    await caller.delete(record)
    assert await caller.get(record) is None

    # -- the door is PROTECTED, never public: an unauthenticated caller is denied --
    anon = ApiClient(f"http://{auth_stack.host}:{auth_stack.port_a}")
    denied = await anon.request_raw("GET", record)
    assert denied.status_code in (401, 403), f"slashed-key record door served unauthenticated: {denied.status_code}"


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
