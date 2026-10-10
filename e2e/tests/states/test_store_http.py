"""The platform state store's HTTP surface, driven end to end over a booted stack —
the operator/API door a Studio States page and the ``tai states`` CLI travel.

One composed path on ``core_stack`` walks the whole ledger a real declaration takes:
declare a state with its subject kinds, upload a template and attach it (so a path is
governed by a ``composing`` regime), then drive the record doors — a whole-path
``set`` over the composing path is refused (422), the keyed ``set_by_key`` op is
accepted, the write ledger records the ``api`` door with the touched paths, a
content search finds the subject, a fold aliases one subject onto another, and an
additive migration re-validates every record. A second leg pins the OFF contract:
with no states database bound (``off_stack``), every door — a read and a write
alike — refuses ``501`` with the stable ``states-not-configured`` code rather than
serving an empty or forged answer. A binding door leg saves hooks whose state binding
names template programs: the save resolves a state's named references in one batch
(read off the stack's Postgres wire through a relay tap), and a reference to a template
neither attached nor declared is refused with the platform's own text, storing nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from urllib.parse import quote

import httpx
import pytest

from tai42_e2e import Infra
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import build_replicas_stack
from tai42_e2e.pgwire import PgStatementTap
from tai42_e2e.stack import TaiStack
from tai42_e2e.tcprelay import TcpRelay, wait_relay_ready

# The OFF door's machine-readable refusal code (``operations.states._NOT_CONFIGURED_CODE``).
_NOT_CONFIGURED_CODE = "states-not-configured"


def _record_path(state: str, target_kind: str, target_name: str, kind: str, key: str) -> str:
    # Each segment is percent-encoded (``safe=""`` encodes ``/`` too), the same contract
    # the SDK and CLI apply, so a subject key that carries ``/`` (a thread id) forms one
    # path segment the record doors route to intact rather than splitting.
    parts = [quote(part, safe="") for part in (state, target_kind, target_name, kind, key)]
    return "/api/states/{}/records/{}/{}/{}/{}".format(*parts)


@pytest.mark.needs("kind:states")
async def test_core_stack_composed_state_store_path(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    state = uniq("status")
    template = uniq("box-mod").replace("_", "-")

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
    assert served["attachments"] == []

    # -- upload a template and attach it (a ``composing`` regime on box.items) -----
    await api.put(
        f"/api/state-templates/{template}",
        json={
            "kind": "state-template",
            "name": template,
            "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
            "regimes": [{"path": ["items"], "regime": "composing"}],
        },
    )
    await api.put(
        f"/api/states/{state}/attachments/{template}",
        json={"path": ["box"], "parameters": {}, "declarations": {}},
    )
    served = await api.get(f"/api/states/{state}")
    assert [m["template"] for m in served["attachments"]] == [template]
    assert {"path": ["box", "items"], "regime": "composing"} in served["regimes"]
    # The single read serves ``updated_at`` as a parseable ISO string through the real JSON
    # encoder (a raw datetime would 500 here) — the same field the list serves.
    datetime.fromisoformat(served["updated_at"].replace("Z", "+00:00"))

    # -- the list serves each declaration's ``updated_at`` (the Updated column) --
    listed = await api.get("/api/states")
    row = next(d for d in listed if d["name"] == state)
    datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))

    # -- the template catalog serves ``attached_to`` + ``shipped_default`` ----------
    templates = await api.get("/api/state-templates")
    template_row = next(m for m in templates if m["name"] == template)
    assert template_row["attached_to"] == 1
    # An operator-uploaded template is not a shipped default.
    assert template_row["shipped_default"] is False

    # -- the attach read door serves the same envelope row the list serves -------
    attachment_rows = await api.get(f"/api/states/{state}/attachments")
    assert [m["template"] for m in attachment_rows] == [template]
    one_attachment = await api.get(f"/api/states/{state}/attachments/{template}")
    assert one_attachment == attachment_rows[0]
    # A template not attached on the state is a 404 at the read door (never an empty 200).
    absent = uniq("absent-mod").replace("_", "-")
    resp = await api.request_raw("GET", f"/api/states/{state}/attachments/{absent}")
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


@pytest.mark.needs("kind:states")
async def test_core_stack_mount_check_reads_effective_parameters(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """Over the real attach door, a template's declarations ``check`` reads the attach's
    effective parameters as ``$parameters``: a declaration exceeding the supplied ``limit``
    is refused 422 with the check's message, and one within it attachments."""
    api = core_stack.api()
    state = uniq("status")
    template = uniq("capped-mod").replace("_", "-")

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
        f"/api/state-templates/{template}",
        json={
            "kind": "state-template",
            "name": template,
            "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
            "parameters": {"limit": {"schema": {"type": "integer"}, "default": 5}},
            "declarations": {
                "schema": {"type": "object", "properties": {"count": {"type": "integer"}}},
                "check": {
                    "content": 'if .count <= $parameters.limit then true else "count exceeds the attach limit" end'
                },
            },
        },
    )

    # A declaration exceeding the supplied limit is refused at the attach door.
    resp = await api.request_raw(
        "PUT",
        f"/api/states/{state}/attachments/{template}",
        json={"path": ["box"], "parameters": {"limit": 8}, "declarations": {"count": 9}},
    )
    assert resp.status_code == 422, resp.text
    assert "count exceeds the attach limit" in resp.text

    # Within the limit, the attach is accepted and served.
    await api.put(
        f"/api/states/{state}/attachments/{template}",
        json={"path": ["box"], "parameters": {"limit": 8}, "declarations": {"count": 7}},
    )
    served = await api.get(f"/api/states/{state}")
    assert [m["template"] for m in served["attachments"]] == [template]


@pytest.mark.needs("kind:states")
async def test_core_stack_template_parameter_with_a_null_default_round_trips(
    core_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """Over the upload door, a parameter whose default is an explicit ``null`` is saved and served
    back with ``"default": null`` (a parameter with no default serves no ``default`` key), an
    attach that supplies only the no-default parameter renders the ``null`` default into the
    effective schema, and a declarations edit re-reads the template without refusing it."""
    api = core_stack.api()
    state = uniq("status")
    template = uniq("nullable-mod").replace("_", "-")
    parameters = {
        "unused": {"schema": {"type": ["object", "null"]}, "default": None},
        "label": {"schema": {"type": ["string", "null"]}, "default": None},
        "item": {"schema": {"type": "object"}},
    }

    await api.put(
        f"/api/states/{state}",
        json={
            "description": "e2e nullable defaults",
            "schema": {"type": "object", "properties": {"note": {"type": "string"}}},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
        },
    )
    saved = await api.put(
        f"/api/state-templates/{template}",
        json={
            "kind": "state-template",
            "name": template,
            "parameters": parameters,
            "schema": {
                "type": "object",
                "properties": {
                    "label": {"type": ["string", "null"], "default": {"$parameter": "label"}},
                    "item": {"$parameter": "item"},
                },
            },
            "declarations": {"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
            "reconcile": {"orphans": {"content": "[]"}, "close": {"content": "."}, "resolutions": {"content": "[]"}},
        },
    )
    assert saved["parameters"] == parameters
    assert (await api.get(f"/api/state-templates/{template}"))["parameters"] == parameters

    await api.put(
        f"/api/states/{state}/attachments/{template}",
        json={"path": ["box"], "parameters": {"item": {"type": "integer"}}, "declarations": {"n": 1}},
    )
    box = (await api.get(f"/api/states/{state}"))["effective_schema"]["properties"]["box"]
    assert box["properties"]["label"] == {"type": ["string", "null"], "default": None}
    assert box["properties"]["item"] == {"type": "integer"}

    resp = await api.request_raw(
        "PATCH", f"/api/states/{state}/attachments/{template}", json={"declarations": {"n": 2}}
    )
    assert resp.status_code == 200, resp.text
    (row,) = await api.get(f"/api/states/{state}/attachments")
    assert row["declarations"] == {"n": 2}


@pytest.mark.needs("kind:states")
async def test_core_stack_template_jq_input_and_update(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """The ``template_jq`` record sub-actions, end to end over the API door: a template
    declares input programs (named reads) and update programs (record operations) on a
    composing path; a GET evaluates an input program for a subject, and a POST applies an
    update program through the same ``apply`` chokepoint as a delta (so the composing regime
    holds). An input program reads the record and returns a value, writing nothing."""
    api = core_stack.api()
    state = uniq("status")
    template = uniq("planner-tmpl").replace("_", "-")

    await api.put(
        f"/api/states/{state}",
        json={
            "description": "e2e template_jq",
            "schema": {"type": "object", "properties": {"note": {"type": "string"}}},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
        },
    )
    await api.put(
        f"/api/state-templates/{template}",
        json={
            "kind": "state-template",
            "name": template,
            "schema": {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}}
                },
            },
            "regimes": [{"path": ["items"], "regime": "composing"}],
            "template_jq": {
                "ids": {
                    "purpose": "input",
                    "description": "the item ids",
                    "jq": {"content": "[(.items // [])[] | .id]"},
                },
                "at_least": {
                    "purpose": "input",
                    "params": ["min"],
                    "jq": {"content": "((.items // []) | length) >= $params.min"},
                },
                "size": {
                    "purpose": "input",
                    "description": "the item count",
                    "jq": {"content": "(.items // []) | length"},
                },
                "add": {
                    "purpose": "update",
                    "description": "add an item",
                    "writes": [["items"]],
                    "jq": {"content": '[{op: "set_by_key", path: ["items"], key_field: "id", value: $input}]'},
                },
            },
        },
    )
    await api.put(
        f"/api/states/{state}/attachments/{template}",
        json={"path": ["box"], "parameters": {}, "declarations": {}},
    )

    record = _record_path(state, "agent", "a-42", "thread", "t1")

    # -- POST an update program: it applies through the composing regime --------
    applied = await api.post(f"{record}/template-jq/add", json={"input": {"id": "a-42", "label": "status"}})
    assert applied["name"] == "add"
    assert applied["applied"] is True
    assert applied["data"]["box"]["items"] == [{"id": "a-42", "label": "status"}]

    # -- GET an input program's result for the subject -------------------------
    ids = await api.get(f"{record}/template-jq/ids")
    assert ids == {"name": "ids", "purpose": "input", "value": ["a-42"]}
    # An input program with a declared param reads it from the query string (JSON-encoded).
    at_least = await api.get(f"{record}/template-jq/at_least?min=1")
    assert at_least["value"] is True

    # -- GET an input program returns its value, writes nothing ----------------
    size = await api.get(f"{record}/template-jq/size")
    assert size["value"] == 1
    # The write ledger still shows exactly one write (the update program), not the input read.
    writes = await api.get(f"{record}/writes")
    assert len(writes["items"]) == 1
    assert writes["items"][0]["origin"]["meta"] == {"template_jq": "add"}

    # -- an unknown program is a 404 (never an empty 200) ----------------------
    resp = await api.request_raw("GET", f"{record}/template-jq/nope")
    assert resp.status_code == 404, resp.text


@pytest.mark.needs("kind:states")
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


@pytest.mark.needs("kind:identity", "kind:states", "setting:seeded-access-control")
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
    anon = ApiClient(auth_stack.origin(auth_stack.port_a))
    denied = await anon.request_raw("GET", record)
    assert denied.status_code in (401, 403), f"slashed-key record door served unauthenticated: {denied.status_code}"


@pytest.mark.needs("kind:states", "topology:replicas", "process", "store:postgres", "probe-tools")
async def test_a_door_binding_save_resolves_its_named_template_references_in_one_batch(
    infra: Infra, fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """The hook register door saves a state binding whose injections name template programs.
    The stack reaches Postgres through a relay whose tap records every statement it sends, and
    every save goes to replica A (one process), so after a first save its catalog caches are
    warm: a save naming two programs then sends exactly as many statements carrying the state's
    name as a save naming one — the two references resolve in one batch, not one read each. A
    reference to a template neither attached nor declared by the binding is refused with the
    platform's own text, and the refused save stores no hook and attaches nothing."""
    tap = PgStatementTap()
    pg_relay = TcpRelay(infra.settings.pg_host, infra.settings.pg_port, observe_client=tap.observer)
    try:
        pg_relay.start()
        wait_relay_ready(pg_relay)
    except BaseException:
        pg_relay.stop()
        raise
    stack = fresh_stack(
        build_replicas_stack,
        resource_kwargs={"pg_host": pg_relay.listen_host, "pg_port": pg_relay.port},
        relays=[pg_relay],
    )
    api = stack.api(port=stack.port_a)
    state = uniq("status")
    template = uniq("counts-tmpl").replace("_", "-")
    absent = uniq("absent-tmpl").replace("_", "-")

    await api.put(
        f"/api/states/{state}",
        json={
            "description": "e2e door binding",
            "schema": {"type": "object", "properties": {"note": {"type": "string"}}},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
        },
    )
    await api.put(
        f"/api/state-templates/{template}",
        json={
            "kind": "state-template",
            "name": template,
            "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
            "template_jq": {
                "ids": {"purpose": "input", "jq": {"content": "[(.items // [])[] | .id]"}},
                "size": {"purpose": "input", "jq": {"content": "(.items // []) | length"}},
            },
        },
    )

    def hook(name: str, references: list[str]) -> dict:
        binding = {
            "states": [
                {
                    "state": state,
                    "templates": [template],
                    "subject_expr": {"content": ".key"},
                    "input_injections": [
                        {"template_jq": reference, "into": f"in_{i}"} for i, reference in enumerate(references)
                    ],
                }
            ]
        }
        return {
            "name": name,
            "topic": uniq("door-topic").replace("_", "-"),
            "tool": "e2e_echo",
            "execution_key": uniq("door-exec"),
            "state_binding": binding,
        }

    async def save_counting(name: str, references: list[str]) -> int:
        mark = tap.mark()
        await api.post("/api/hooks", json=hook(name, references))
        return sum(1 for statement in tap.since(mark) if statement.carries(state))

    # The first save attaches the template on use and warms replica A's catalog caches.
    first = uniq("door-hook").replace("_", "-")
    await save_counting(first, ["ids", "size"])
    served = await api.get(f"/api/states/{state}")
    assert [m["template"] for m in served["attachments"]] == [template]

    one_reference = await save_counting(uniq("door-hook").replace("_", "-"), ["ids"])
    two_references = await save_counting(uniq("door-hook").replace("_", "-"), [f"{template}.ids", "size"])
    assert one_reference > 0, "the tap recorded no statement carrying the state's name"
    assert two_references == one_reference, (
        f"a save naming two programs sent {two_references} statements carrying the state's name, "
        f"one naming a single program {one_reference}"
    )
    hooks_before = {item["name"] for item in (await api.get("/api/hooks"))["items"]}
    assert first in hooks_before

    # A reference to a template the binding neither attaches nor declares is refused, after the
    # valid reference before it, with the platform's text; nothing is stored or attached.
    refused_name = uniq("door-hook").replace("_", "-")
    refused = await api.request_raw("POST", "/api/hooks", json=hook(refused_name, ["ids", f"{absent}.size"]))
    assert refused.status_code == 404, refused.text
    assert refused.json()["error"] == (
        f"invalid hook params: template {absent!r} is not attached on state {state!r}"
    ), refused.text
    hooks_after = {item["name"] for item in (await api.get("/api/hooks"))["items"]}
    assert hooks_after == hooks_before
    served = await api.get(f"/api/states/{state}")
    assert [m["template"] for m in served["attachments"]] == [template]


async def _assert_states_off(off_stack: TaiStack, method: str, path: str, *, json=None) -> None:
    api: ApiClient = off_stack.api()
    resp: httpx.Response = await api.request_raw(method, path, json=json)
    assert resp.status_code == 501, f"{method} {path} -> {resp.status_code}; body: {resp.text}"
    body = resp.json()
    assert body.get("code") == _NOT_CONFIGURED_CODE, f"{method} {path} code={body.get('code')!r}; body: {resp.text}"
    assert isinstance(body.get("error"), str), resp.text
    assert body["error"], resp.text


@pytest.mark.needs("setting:states=off")
async def test_off_stack_state_doors_refuse_501(off_stack: TaiStack) -> None:
    # A read door — no empty-degrade for the record store; it refuses loudly.
    await _assert_states_off(off_stack, "GET", "/api/states")
    await _assert_states_off(off_stack, "GET", "/api/state-templates")
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
    # A view read door and a rule write door.
    await _assert_states_off(off_stack, "GET", "/api/states/status-off/records/agent/a-42/thread/t1/template-jq/ids")
    await _assert_states_off(
        off_stack,
        "POST",
        "/api/states/status-off/records/agent/a-42/thread/t1/template-jq/add",
        json={"input": {"id": "x"}},
    )


@pytest.mark.needs("kind:states", "topology:replicas")
async def test_a_template_replace_on_one_worker_is_served_on_the_other(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """Every per-process derived artifact of a template keys on its stored version: a replace
    through replica A is served — document, attached declaration, and the program it evaluates —
    by replica B on its next read, with no fan-out."""
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    state = uniq("status")
    template = uniq("replace-tmpl").replace("_", "-")

    def _template(answer: str, regime: str) -> dict:
        return {
            "kind": "state-template",
            "name": template,
            "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
            "regimes": [{"path": ["items"], "regime": regime}],
            "template_jq": {"answer": {"purpose": "input", "jq": {"content": answer}}},
        }

    await api_a.put(
        f"/api/states/{state}",
        json={
            "schema": {"type": "object", "properties": {"note": {"type": "string"}}},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
        },
    )
    await api_a.put(f"/api/state-templates/{template}", json=_template('"before"', "single"))
    await api_a.put(f"/api/states/{state}/attachments/{template}", json={"path": ["box"]})
    record = _record_path(state, "agent", "a-42", "thread", "t1")
    # B reads (and caches) the template, the declaration and the rendered program first.
    assert (await api_b.get(f"/api/state-templates/{template}"))["regimes"] == [{"path": ["items"], "regime": "single"}]
    assert (await api_b.get(f"/api/states/{state}"))["regimes"] == [{"path": ["box", "items"], "regime": "single"}]
    assert (await api_b.get(f"{record}/template-jq/answer"))["value"] == "before"

    await api_a.put(f"/api/state-templates/{template}?replace=true", json=_template('"after"', "composing"))

    assert (await api_b.get(f"/api/state-templates/{template}"))["regimes"] == [
        {"path": ["items"], "regime": "composing"}
    ]
    assert (await api_b.get(f"/api/states/{state}"))["regimes"] == [{"path": ["box", "items"], "regime": "composing"}]
    assert (await api_b.get(f"{record}/template-jq/answer"))["value"] == "after"


@pytest.mark.needs("kind:states", "topology:replicas")
async def test_a_state_and_template_recreated_on_one_worker_are_served_anew_on_the_other(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A catalog version never repeats for a name: a state and a template deleted and re-created
    through replica A are served by replica B as the re-created rows — B's next write validates
    under the new schema and its rendered program is the new one — though B cached the deleted
    rows first."""
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    state = uniq("status")
    template = uniq("recreate-tmpl").replace("_", "-")
    record = _record_path(state, "agent", "a-42", "thread", "t1")

    async def create(note_type: str, answer: str) -> None:
        await api_a.put(
            f"/api/states/{state}",
            json={
                "schema": {"type": "object", "properties": {"note": {"type": note_type}}},
                "subject_kinds": ["thread"],
                "default_subject_kind": "thread",
            },
        )
        await api_a.put(
            f"/api/state-templates/{template}",
            json={
                "kind": "state-template",
                "name": template,
                "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
                "template_jq": {"answer": {"purpose": "input", "jq": {"content": answer}}},
            },
        )
        await api_a.put(f"/api/states/{state}/attachments/{template}", json={"path": ["box"]})

    await create("integer", '"before"')
    # B writes and evaluates first, so it holds the declaration, its validator and the rendered program.
    await api_b.put(record, json={"note": 1})
    assert (await api_b.get(f"{record}/template-jq/answer"))["value"] == "before"

    await api_a.delete(f"/api/states/{state}/attachments/{template}")
    await api_a.delete(f"/api/state-templates/{template}")
    await api_a.delete(f"/api/states/{state}")
    await create("string", '"after"')

    await api_b.put(record, json={"note": 2}, expect=422)
    await api_b.put(record, json={"note": "two"})
    assert (await api_b.get(f"{record}/template-jq/answer"))["value"] == "after"


@pytest.mark.needs("kind:states")
async def test_a_shipped_template_seed_reads_back(seams_stack: TaiStack) -> None:
    """A template a plugin ships as a seed is stored through the validated write, so it reads
    back like an uploaded one — typed sections, the trace default included."""
    api = seams_stack.api()
    served = await api.get("/api/state-templates/e2e-seed-template")
    assert served["name"] == "e2e-seed-template"
    assert served["parameters"] == {"cap": {"schema": {"type": "integer"}, "default": 3}}
    assert served["trace"] == {"enabled": False}
    catalog = await api.get("/api/state-templates")
    listed = next(item for item in catalog if item["name"] == "e2e-seed-template")
    assert listed["shipped_default"] is True
