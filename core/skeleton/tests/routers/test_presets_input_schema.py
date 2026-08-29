"""The preset ``input_schema`` front-door over the REAL engine + store.

Drives the presets route handlers directly (the router-test pattern) inside a live
``app.app_context`` with the true ``PostgresVersionedStore`` + ``PresetStoreView``
over the stateful fake Postgres and the real ``PresetManager``. The manifest
registers ``payload_tool`` — a base tool with a structured ``payload`` argument — for
which the tests register ``PresetInputSchemaSupport(payload_arg="payload")`` so the
authored ``input_schema`` becomes the exposed tool's input contract, plus ``weather``
(no input-schema support) so the unsupported-base authoring rejection is reachable.

These pin the SAVE-VERSION door's ``input_schema`` carry-forward parity with
``output_schema`` (set / clear-with-null / absent-carries), the loud unsupported-base
400, the symmetric rollback restore, the GET round-trip on the record views, the
validate (dry-run) parity, and the studio's ``fixed_kwargs`` side channel regression.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from starlette.requests import Request
from tai42_contract.presets import PresetInputSchemaSupport
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.routers import presets as router

from ..versioning.conftest import FakeVersioningPg

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather", "payload_tool"]}],
}

_IN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": False,
}

_SUPPORTED_BASE = "payload_tool"


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


# -- request / response helpers ----------------------------------------------


def _request(method: str, path: str, *, body: Any = None, query: str = "", **path_params: str) -> Request:
    payload = b"" if body is None else json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "query_string": query.encode(),
        "path_params": path_params,
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


def _data(resp) -> Any:
    return json.loads(bytes(resp.body))["data"]


def _err(resp) -> str:
    return json.loads(bytes(resp.body))["error"]


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def pg(monkeypatch) -> FakeVersioningPg:
    fake = FakeVersioningPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield fake

    monkeypatch.setattr(store_module, "client_ctx", fake_client_ctx)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    return fake


@pytest.fixture(autouse=True)
def _emit(monkeypatch) -> None:
    async def spy(kind: str) -> None:
        return None

    monkeypatch.setattr(instance.app, "emit_list_changed", spy)


@pytest.fixture(autouse=True)
def _reset_preset_registry():
    yield
    mgr = instance.app.preset_manager

    async def _clear() -> None:
        for name in list(mgr.registered_names()):
            await mgr.remove(name)
        provider = instance.app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    for name in list(mgr.quarantined_names()):
        mgr.drop_quarantine(name)


def _declare_support() -> None:
    # The base-tool plugin's own declaration (``register_input_schema_support`` at tool-module
    # load) — the registry is reset on every ``start()``, so the tests re-declare it inside the
    # live context, exactly as a real base-tool module would at import.
    instance.app.presets.register_input_schema_support(_SUPPORTED_BASE, PresetInputSchemaSupport(payload_arg="payload"))


async def _create(name: str, base_tool: str, **over: Any):
    body: dict[str, Any] = {"name": name, "base_tool": base_tool, "description": "an input-schema preset"}
    body.update(over)
    return await router.create_preset(_request("POST", "/api/presets", body=body))


# -- create DECLARE + GET round-trip -----------------------------------------


def test_create_input_schema_advertises_and_round_trips(pg):
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            resp = await _create("wx", _SUPPORTED_BASE, input_schema=_IN_SCHEMA)
            assert resp.status_code == 200, _err(resp)
            # The create response, get, and list all surface the authored input schema
            # (the record views round-trip it symmetrically with output_schema).
            assert _data(resp)["input_schema"] == _IN_SCHEMA
            got = _data(await router.get_preset(_request("GET", "/api/presets/wx", name="wx")))
            assert got["input_schema"] == _IN_SCHEMA
            rows = _data(await router.list_presets(_request("GET", "/api/presets")))
            assert next(r for r in rows if r["name"] == "wx")["input_schema"] == _IN_SCHEMA
            # The exposed tool advertises the authored schema and routes the validated
            # object into ``payload``.
            assert await instance.app.tools.run_tool("wx", {"city": "paris"}) == {
                "payload": {"city": "paris"},
                "tag": "t",
            }

    asyncio.run(run())


# -- SAVE-VERSION door: the three carry-forward modes (parity with output_schema) --


def test_save_version_input_schema_carry_forward_modes(pg):
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            assert (await _create("v", _SUPPORTED_BASE, input_schema=_IN_SCHEMA)).status_code == 200

            # Mode 1 — omitted carries forward (a description-only save keeps the schema).
            r1 = await router.save_version(
                _request("POST", "/api/presets/v/versions", body={"description": "edited"}, name="v")
            )
            assert r1.status_code == 200, _err(r1)
            got = _data(await router.get_preset(_request("GET", "/api/presets/v", name="v")))
            assert got["input_schema"] == _IN_SCHEMA

            # Mode 2 — an explicit new schema wins, and re-binds on load.
            new_schema = {
                "type": "object",
                "properties": {"town": {"type": "string"}},
                "required": ["town"],
                "additionalProperties": False,
            }
            r2 = await router.save_version(
                _request("POST", "/api/presets/v/versions", body={"input_schema": new_schema}, name="v")
            )
            assert r2.status_code == 200, _err(r2)
            got = _data(await router.get_preset(_request("GET", "/api/presets/v", name="v")))
            assert got["input_schema"] == new_schema
            # The rebound tool now validates the caller against the NEW contract.
            assert await instance.app.tools.run_tool("v", {"town": "lyon"}) == {
                "payload": {"town": "lyon"},
                "tag": "t",
            }

            # Mode 3 — an explicit null clears (the exposed tool reverts to the base schema).
            r3 = await router.save_version(
                _request("POST", "/api/presets/v/versions", body={"input_schema": None}, name="v")
            )
            assert r3.status_code == 200, _err(r3)
            got = _data(await router.get_preset(_request("GET", "/api/presets/v", name="v")))
            assert got["input_schema"] is None

            # The version history carries the field on every row (round-trip inside ``body``).
            versions = _data(await router.list_versions(_request("GET", "/api/presets/v/versions", name="v")))
            assert versions[0]["body"]["input_schema"] == _IN_SCHEMA
            assert versions[1]["body"]["input_schema"] == _IN_SCHEMA
            assert versions[2]["body"]["input_schema"] == new_schema
            assert versions[3]["body"]["input_schema"] is None

    asyncio.run(run())


def test_save_version_input_schema_only_body_is_accepted(pg):
    # ``input_schema`` alone satisfies the "at least one of" door gate — an
    # input-schema-only save is accepted.
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            assert (await _create("solo", _SUPPORTED_BASE)).status_code == 200
            resp = await router.save_version(
                _request("POST", "/api/presets/solo/versions", body={"input_schema": _IN_SCHEMA}, name="solo")
            )
            assert resp.status_code == 200, _err(resp)
            got = _data(await router.get_preset(_request("GET", "/api/presets/solo", name="solo")))
            assert got["input_schema"] == _IN_SCHEMA

    asyncio.run(run())


def test_empty_save_version_body_lists_input_schema(pg):
    # The empty-body 400 message now names ``input_schema`` among the accepted fields.
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            assert (await _create("e", _SUPPORTED_BASE)).status_code == 200
            resp = await router.save_version(_request("POST", "/api/presets/e/versions", body={}, name="e"))
            assert resp.status_code == 400
            assert "input_schema" in _err(resp)

    asyncio.run(run())


# -- unsupported base: the loud 400 (never a silent drop) --------------------


def test_save_version_input_schema_over_unsupported_base_400(pg):
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            # ``weather`` declares NO input-schema support, so setting one is a loud 400
            # (never a silent drop).
            assert (await _create("w", "weather")).status_code == 200
            resp = await router.save_version(
                _request("POST", "/api/presets/w/versions", body={"input_schema": _IN_SCHEMA}, name="w")
            )
            assert resp.status_code == 400
            assert "does not accept a preset input_schema" in _err(resp)
            # And the drop-through: the preset is unchanged (no version committed the schema).
            got = _data(await router.get_preset(_request("GET", "/api/presets/w", name="w")))
            assert got["input_schema"] is None

    asyncio.run(run())


def test_create_input_schema_over_unsupported_base_400(pg):
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            resp = await _create("cw", "weather", input_schema=_IN_SCHEMA)
            assert resp.status_code == 400
            assert "does not accept a preset input_schema" in _err(resp)

    asyncio.run(run())


# -- ROLLBACK: symmetric restore of input_schema AND output_schema -----------


def test_rollback_restores_input_schema_from_target_version(pg):
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            # v1 has the schema; v2 clears it. Rolling back to v1 must RESTORE it (the
            # target version's stored body wins — never a carry of the active v2 value).
            assert (await _create("rb", _SUPPORTED_BASE, input_schema=_IN_SCHEMA)).status_code == 200
            r2 = await router.save_version(
                _request("POST", "/api/presets/rb/versions", body={"input_schema": None}, name="rb")
            )
            assert r2.status_code == 200, _err(r2)
            assert _data(await router.get_preset(_request("GET", "/api/presets/rb", name="rb")))["input_schema"] is None

            resp = await router.rollback_preset(
                _request("POST", "/api/presets/rb/rollback", body={"version": 1}, name="rb")
            )
            assert resp.status_code == 200, _err(resp)
            got = _data(await router.get_preset(_request("GET", "/api/presets/rb", name="rb")))
            assert got["input_schema"] == _IN_SCHEMA
            # The live tool rebinds to the restored contract.
            restored = await instance.app.tools.run_tool("rb", {"city": "nice"})
            assert restored == {"payload": {"city": "nice"}, "tag": "t"}

    asyncio.run(run())


# -- VALIDATE (dry-run) parity: mirrors the write door's verdict --------------


def test_validate_input_schema_over_unsupported_base_is_invalid(pg):
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            # CREATE mode: a set input_schema over an unsupported base is an INVALID verdict,
            # never a false "valid" (the dry run mirrors create's authoring rejection).
            resp = await router.validate_preset(
                _request(
                    "POST",
                    "/api/presets/validate",
                    body={"name": "vv", "base_tool": "weather", "description": "d", "input_schema": _IN_SCHEMA},
                )
            )
            assert resp.status_code == 200, _err(resp)
            verdict = _data(resp)
            assert verdict["valid"] is False
            assert "does not accept a preset input_schema" in verdict["error"]

    asyncio.run(run())


def test_validate_input_schema_over_supported_base_is_valid(pg):
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            resp = await router.validate_preset(
                _request(
                    "POST",
                    "/api/presets/validate",
                    body={"name": "vok", "base_tool": _SUPPORTED_BASE, "description": "d", "input_schema": _IN_SCHEMA},
                )
            )
            assert resp.status_code == 200, _err(resp)
            assert _data(resp)["valid"] is True

    asyncio.run(run())


def test_validate_version_mode_input_schema_over_unsupported_base_is_invalid(pg):
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            # VERSION mode (a preset already exists): setting an input_schema on an
            # unsupported base is invalid, mirroring the save-version door.
            assert (await _create("vm", "weather")).status_code == 200
            resp = await router.validate_preset(
                _request("POST", "/api/presets/validate", body={"name": "vm", "input_schema": _IN_SCHEMA})
            )
            assert resp.status_code == 200, _err(resp)
            verdict = _data(resp)
            assert verdict["valid"] is False
            assert "does not accept a preset input_schema" in verdict["error"]

    asyncio.run(run())


# -- studio side channel regression: fixed_kwargs.input_schema is untouched ---


def test_fixed_kwargs_input_schema_channel_unaffected(pg):
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            # The studio's flow presets carry an ``input_schema`` as a plain ``fixed_kwargs``
            # arg of a flow tool — a baked kwarg, NOT the top-level door field. A save that
            # only touches fixed_kwargs must leave the baked value intact and never engage the
            # top-level input-schema authoring gate. ``payload`` is the base tool's own arg, so
            # baking ``input_schema`` under it stands in for that channel.
            baked = {"payload": {"input_schema": {"type": "object"}}}
            assert (await _create("flow", _SUPPORTED_BASE, fixed_kwargs=baked)).status_code == 200
            # A description-only save carries the baked fixed_kwargs forward untouched, and the
            # top-level input_schema stays absent (None) — the two channels never cross.
            resp = await router.save_version(
                _request("POST", "/api/presets/flow/versions", body={"description": "edited"}, name="flow")
            )
            assert resp.status_code == 200, _err(resp)
            got = _data(await router.get_preset(_request("GET", "/api/presets/flow", name="flow")))
            assert got["fixed_kwargs"] == baked
            assert got["input_schema"] is None

    asyncio.run(run())


# -- absent field never engages the gate over an unsupported base ------------


def test_save_version_absent_input_schema_over_unsupported_base_ok(pg):
    # A save that does not mention input_schema over an unsupported base is fine (carry-forward
    # of ``None`` never trips the authoring gate) — proves the presence flag, not ``None``,
    # drives the sentinel.
    async def run():
        async with instance.app.app_context(_manifest()):
            _declare_support()
            assert (await _create("keep", "weather")).status_code == 200
            resp = await router.save_version(
                _request("POST", "/api/presets/keep/versions", body={"description": "edited"}, name="keep")
            )
            assert resp.status_code == 200, _err(resp)

    asyncio.run(run())
