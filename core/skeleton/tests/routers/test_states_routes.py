"""The states routes as thin adapters over the ops + facet.

Full record CRUD runs against a live Postgres in the e2e suite; here the unit surface pins
what needs no database: every route is registered at its expected ``(method, path)``, the
``/api/state-modules`` sibling keeps the modules OFF the ``/api/states/{name}`` template
position (a state literally named ``modules`` stays reachable at ``GET /api/states/modules``
while ``GET /api/state-modules`` lists modules), and every door refuses 501
``states-not-configured`` while the store is unbound.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

import pytest
from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import NarrowingRequiresConfirmationError, StatesError
from tai42_contract.states.models import (
    CompletedOrigin,
    ConsumerLink,
    ConsumerRow,
    StateDeclaration,
    WriteEntry,
    WritesPage,
)

from tai42_skeleton.app import instance
from tai42_skeleton.app.route_registry import load_api_routes
from tai42_skeleton.operations import NotSupportedError, PreconditionFailedError, ValidationRejected
from tai42_skeleton.operations import states as ops
from tai42_skeleton.routers import states as router

tai42_app.bind(instance.build_app())


def _run[T](awaitable: Awaitable[T]) -> T:
    """Drive a route handler's ``Awaitable[Response]`` to its result on a fresh loop —
    a handler is typed ``Awaitable``, not ``Coroutine``, so it is awaited inside a
    coroutine ``asyncio.run`` can accept."""

    async def _await() -> T:
        return await awaitable

    return asyncio.run(_await())


def _routes_by_name() -> dict[str, tuple[str, str]]:
    """``op name -> (method, path)`` for every registered states route."""
    out: dict[str, tuple[str, str]] = {}
    for meta in load_api_routes():
        if meta.path.startswith("/api/states") or meta.path.startswith("/api/state-"):
            for method in meta.methods:
                out[f"{meta.name}:{method}"] = (method, meta.path)
    return out


_EXPECTED: set[tuple[str, str]] = {
    ("GET", "/api/states"),
    ("GET", "/api/states/{name}"),
    ("PUT", "/api/states/{name}"),
    ("DELETE", "/api/states/{name}"),
    ("GET", "/api/states/{name}/stats"),
    ("POST", "/api/states/{name}/migrate"),
    ("POST", "/api/states/{name}/migrate/preview"),
    ("GET", "/api/states/{name}/mounts"),
    ("PUT", "/api/states/{name}/mounts/{module}"),
    ("PATCH", "/api/states/{name}/mounts/{module}"),
    ("DELETE", "/api/states/{name}/mounts/{module}"),
    ("GET", "/api/states/{name}/subjects"),
    ("POST", "/api/states/{name}/records/search"),
    ("GET", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"),
    ("PUT", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"),
    ("PATCH", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"),
    ("POST", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}/deltas"),
    ("DELETE", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}"),
    ("POST", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}/fold"),
    ("GET", "/api/states/{name}/records/{target_kind}/{target_name}/{kind}/{key}/writes"),
    ("GET", "/api/states/{name}/consumers"),
    ("GET", "/api/state-modules"),
    ("GET", "/api/state-modules/{name}"),
    ("PUT", "/api/state-modules/{name}"),
    ("DELETE", "/api/state-modules/{name}"),
    ("POST", "/api/state-retention/prune"),
}


def test_every_states_route_is_registered() -> None:
    registered = {pair for meta in load_api_routes() for pair in ((m, meta.path) for m in meta.methods)}
    missing = sorted(_EXPECTED - registered)
    assert not missing, f"states routes not registered: {missing}"


def test_modules_sibling_keeps_the_state_name_reachable() -> None:
    # NOTHING literal sits on the /api/states/{name} template position: a state named
    # ``modules`` resolves to the state read, while modules live at the sibling collection.
    by_path: dict[tuple[str, str], str] = {}
    for meta in load_api_routes():
        for method in meta.methods:
            by_path[(method, meta.path)] = meta.name
    assert by_path[("GET", "/api/states/{name}")] == "get_state"
    assert by_path[("GET", "/api/state-modules")] == "list_state_modules"
    # No route sits literally at /api/states/modules — a state so named hits the template.
    assert ("GET", "/api/states/modules") not in by_path


def _request(method: str, path: str, **path_params: str) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "path_params": path_params,
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def test_list_states_operation_refuses_501_when_unbound() -> None:
    # With no states database bound, the read door refuses loudly rather than serving empty.
    with pytest.raises(NotSupportedError) as excinfo:
        asyncio.run(ops.list_states())
    assert excinfo.value.status == 501
    assert excinfo.value.extra.get("code") == "states-not-configured"


def test_route_answers_501_body_with_the_stable_code_when_unbound() -> None:
    async def _call():
        return await router.list_states(_request("GET", "/api/states"))

    resp = asyncio.run(_call())
    assert resp.status_code == 501
    body = json.loads(bytes(resp.body))
    assert body["code"] == "states-not-configured"


# --------------------------------------------------------------------------- #
# The error -> status chokepoint (_states_door + _ERROR_MAP) and the flat      #
# operation surface, exercised through a fake facet so no database is needed.  #
# --------------------------------------------------------------------------- #
class _FakeStates:
    """A stand-in for ``instance.app.states`` covering only the methods a test drives; each
    installs the outcome (a raise or a value) the door is asserted to map."""

    def __init__(self) -> None:
        self.migrate_calls: list[dict[str, Any]] = []
        self.writes_calls: list[dict[str, Any]] = []

    async def migrate(self, name, new_schema, *, origin, transform_expr, confirm_drop, resolutions):
        self.migrate_calls.append({"confirm_drop": confirm_drop})
        if not confirm_drop:
            raise NarrowingRequiresConfirmationError("a narrowing change requires EXACTLY ONE of ...")

    async def writes(self, state, subject, *, limit, cursor):
        self.writes_calls.append({"limit": limit, "cursor": cursor})
        return WritesPage(
            items=[
                WriteEntry(
                    seq=2.0,
                    at=datetime(2026, 1, 2, tzinfo=UTC),
                    origin=CompletedOrigin(consumer="flow", op_id="op-2", door="tool"),
                    paths=[["a"], ["b", 0]],
                ),
                WriteEntry(
                    seq=1.0,
                    at=datetime(2026, 1, 1, tzinfo=UTC),
                    origin=CompletedOrigin(door="api", actor="u-1"),
                    paths=[["a"]],
                ),
            ],
            next_cursor="17",
        )

    async def list_modules_catalog(self):
        return [
            {"kind": "state-module", "name": "tagmod", "schema": {}, "mounted_on": 2, "shipped_default": True},
            {"kind": "state-module", "name": "loose", "schema": {}, "mounted_on": 0, "shipped_default": False},
        ]

    async def list_declarations(self):
        raise _UnmappedStatesError("a brand-new store error class the map does not know")

    async def consumers(self, name):
        return [
            ConsumerRow(kind="hook", name="on-alert", detail="supplies kind person", link=ConsumerLink(token="hooks")),
            ConsumerRow(kind="schedule", unavailable="no scheduling backend"),
            ConsumerRow(kind="agent", name="assistant", detail="uses state_read", link=ConsumerLink(token="agents")),
            ConsumerRow(kind="preset", name="greeter", detail="binds state_merge", link=ConsumerLink(token="presets")),
        ]


class _UnmappedStatesError(StatesError):
    """A StatesError absent from ``_ERROR_MAP`` — the door must re-raise it, never mask a
    new store fault as a silent 500."""


def _install_fake_states(monkeypatch: pytest.MonkeyPatch) -> _FakeStates:
    fake = _FakeStates()
    monkeypatch.setattr(ops, "_states", lambda: fake)
    return fake


def test_migrate_maps_narrowing_to_412_then_confirm_drop_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_states(monkeypatch)
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    with pytest.raises(PreconditionFailedError) as excinfo:
        asyncio.run(ops.migrate_state("alerts", schema))
    assert excinfo.value.status == 412
    # The store's own message reaches the client verbatim through the map.
    assert "EXACTLY ONE" in str(excinfo.value)
    # The retry that confirms the drop passes through to a success body.
    out = asyncio.run(ops.migrate_state("alerts", schema, confirm_drop=True))
    assert out == {"migrated": True, "name": "alerts"}
    assert [c["confirm_drop"] for c in fake.migrate_calls] == [False, True]


def test_malformed_record_path_segment_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bad target_kind (not a ConversationTargetKind) fails the subject parse at the edge,
    # before the facet is reached — a 422 rejected input, never a 500.
    _install_fake_states(monkeypatch)
    with pytest.raises(ValidationRejected) as excinfo:
        asyncio.run(ops.read_state_record("alerts", "not-a-target-kind", "acme", "person", "p-1"))
    assert excinfo.value.status == 422


def test_writes_listing_pages_through_the_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_states(monkeypatch)
    out = asyncio.run(ops.list_state_writes("alerts", "agent", "a", "person", "p-1", limit=2, cursor="c-0"))
    # The keyset page's limit/cursor reach the facet unchanged.
    assert fake.writes_calls == [{"limit": 2, "cursor": "c-0"}]
    # The page wraps the serialized items (newest first) and the next keyset cursor.
    assert out["next_cursor"] == "17"
    items = out["items"]
    assert [e["seq"] for e in items] == [2.0, 1.0]
    assert items[0]["origin"]["consumer"] == "flow"
    assert items[0]["origin"]["door"] == "tool"
    assert items[0]["paths"] == [["a"], ["b", 0]]
    assert items[1]["origin"]["door"] == "api"


class _ServingStates:
    """A facet that serves a real declaration carrying ``updated_at`` (a datetime), for the
    route-level encoder regression: the list and single reads must reach the client through
    the JSON response encoder, so a raw datetime would 500 unless the op dumps ``mode=json``."""

    async def list_declarations(self):
        return [
            StateDeclaration(
                name="alerts",
                schema={"type": "object"},
                subject_kinds=["thread"],
                default_subject_kind="thread",
                updated_at=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
            )
        ]

    async def served_declaration(self, name):
        return {
            "name": name,
            "description": "",
            "schema": {"type": "object"},
            "effective_schema": {"type": "object"},
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
            "mounts": [],
            "regimes": [],
            "updated_at": "2026-09-06T12:00:00Z",
        }


def test_declaration_reads_serve_updated_at_as_an_iso_string_through_the_encoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ops, "_states", lambda: _ServingStates())
    # The list route encodes its body through the real JSON encoder — a raw datetime would
    # raise here; ``mode="json"`` renders ``updated_at`` an ISO string the response carries.
    list_resp = _run(router.list_states(_request("GET", "/api/states")))
    assert list_resp.status_code == 200
    listed = json.loads(bytes(list_resp.body))["data"]
    assert listed[0]["updated_at"] == "2026-09-06T12:00:00Z"
    # The single read serves the same ISO timestamp.
    get_resp = _run(router.get_state(_request("GET", "/api/states/alerts", name="alerts")))
    assert get_resp.status_code == 200
    got = json.loads(bytes(get_resp.body))["data"]
    # The single read serves the same ISO ``…Z`` format as the list, never a second shape.
    assert got["updated_at"] == "2026-09-06T12:00:00Z"


def test_module_listing_serves_the_catalog_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_states(monkeypatch)
    out = asyncio.run(ops.list_state_modules())
    # Each document carries the catalog columns the module screen reads.
    by_name = {row["name"]: row for row in out}
    assert by_name["tagmod"]["mounted_on"] == 2
    assert by_name["tagmod"]["shipped_default"] is True
    assert by_name["loose"]["mounted_on"] == 0
    assert by_name["loose"]["shipped_default"] is False


def test_unmapped_store_error_reraises_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_states(monkeypatch)
    # An unmapped StatesError propagates as itself — the door refuses to mask a new store
    # fault behind a silent generic error.
    with pytest.raises(_UnmappedStatesError):
        asyncio.run(ops.list_states())


def test_consumers_union_serializes_every_family_including_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_states(monkeypatch)
    rows = asyncio.run(ops.state_consumers("alerts"))
    kinds = [r["kind"] for r in rows]
    assert kinds == ["hook", "schedule", "agent", "preset"]
    # The muted, cannot-list row survives serialization (surfaced, never swallowed).
    schedule_row = next(r for r in rows if r["kind"] == "schedule")
    assert schedule_row["unavailable"] == "no scheduling backend"
    assert schedule_row["name"] is None
    hook_row = next(r for r in rows if r["kind"] == "hook")
    assert hook_row["link"]["token"] == "hooks"
    preset_row = next(r for r in rows if r["kind"] == "preset")
    assert preset_row["detail"] == "binds state_merge"
    assert preset_row["link"]["token"] == "presets"
