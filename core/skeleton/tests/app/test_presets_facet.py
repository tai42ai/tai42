"""The in-process preset create / save-version facet doors (``app.presets.create`` /
``app.presets.save_version``).

These drive the facet over the REAL engine + the true ``PostgresVersionedStore`` /
``PresetStoreView`` on the stateful fake Postgres (the ``pg`` fixture) and the real
``PresetManager`` — so the in-process door runs the identical content path (name
pre-checks, write validators, input-schema support) and returns the same record view
as the HTTP door, proven by comparing the two doors' outputs for one input.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from starlette.requests import Request
from tai42_contract.presets.errors import PresetNotFoundError
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.routers import presets as router

from ..versioning.conftest import FakeVersioningPg

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather", "echo"]}],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


# -- request / response helpers ----------------------------------------------


def _request(method: str, path: str, *, body: Any = None, **path_params: str) -> Request:
    payload = b"" if body is None else json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "path_params": path_params,
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


def _data(resp) -> Any:
    return json.loads(bytes(resp.body))["data"]


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
def _reset_preset_registry():
    """Tear down every runtime-registered / quarantined preset after each test — the
    singleton ``PresetManager`` outlives one ``app_context``, so a bound preset or base
    tool would collide with the next test's manifest bind."""
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


# -- create ------------------------------------------------------------------


def test_create_persists_and_registers(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            view = await instance.app.presets.create("fp", "weather", "d", {"units": "imperial"})
            assert view["name"] == "fp"
            assert view["active_version"] == 1
            assert view["conflicted"] is False
            assert "fanout" in view
            # The binding serves the baked kwargs — created AND registered.
            assert await instance.app.tools.run_tool("fp", {"city": "x"}) == {
                "city": "x",
                "units": "imperial",
            }

    asyncio.run(run())


def test_create_runs_write_validator_like_http_door(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def validator(body):
                return ["weather rejects this"]

            instance.app.presets.register_write_validator("weather", validator)
            with pytest.raises(BadRequestError) as exc_info:
                await instance.app.presets.create("fp", "weather", "d", {"units": "v"})
            assert exc_info.value.message == "weather rejects this"
            # A rejected create persisted no row.
            with pytest.raises(PresetNotFoundError):
                await instance.app.presets.store.get_preset("fp")

    asyncio.run(run())


def test_create_consults_input_schema_support(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # ``weather`` registers no input-schema support, so an input_schema is the
            # same loud 400 the HTTP door raises, and no row persists.
            with pytest.raises(BadRequestError) as exc_info:
                await instance.app.presets.create("fp", "weather", "d", {"units": "v"}, input_schema={"type": "object"})
            assert "input_schema" in exc_info.value.message
            with pytest.raises(PresetNotFoundError):
                await instance.app.presets.store.get_preset("fp")

    asyncio.run(run())


def test_create_response_shape_equals_http_door(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            http_view = _data(
                await router.create_preset(
                    _request(
                        "POST",
                        "/api/presets",
                        body={"name": "rp", "base_tool": "weather", "description": "d", "fixed_kwargs": {"units": "v"}},
                    )
                )
            )
            facet_view = await instance.app.presets.create("fp", "weather", "d", {"units": "v"})
            assert set(facet_view) == set(http_view)
            # Every field but the identity is identical for the identical input.
            assert {k: v for k, v in facet_view.items() if k != "name"} == {
                k: v for k, v in http_view.items() if k != "name"
            }

    asyncio.run(run())


# -- save_version ------------------------------------------------------------


def test_save_version_creates_new_active_keeping_earlier(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await instance.app.presets.create("fp", "weather", "d", {"units": "v1"})
            row = await instance.app.presets.save_version("fp", fixed_kwargs={"units": "v2"})
            assert row["version"] == 2
            assert "fanout" in row
            # The new version is active and serves the new kwargs.
            assert await instance.app.tools.run_tool("fp", {"city": "x"}) == {"city": "x", "units": "v2"}
            # Version 1 is kept as history.
            versions = await instance.app.presets.store.list_versions("fp")
            assert sorted(v.version for v in versions) == [1, 2]

    asyncio.run(run())


def test_save_version_response_shape_equals_http_door(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await instance.app.presets.create("fp", "weather", "d", {"units": "v1"})
            facet_row = await instance.app.presets.save_version("fp", fixed_kwargs={"units": "v2"})
            http_row = _data(
                await router.save_version(
                    _request(
                        "POST",
                        "/api/presets/fp/versions",
                        name="fp",
                        body={"fixed_kwargs": {"units": "v3"}},
                    )
                )
            )
            assert set(facet_row) == set(http_row)

    asyncio.run(run())


def test_save_version_absent_name_raises(pg) -> None:
    from tai42_skeleton.operations import NotFoundError

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            with pytest.raises(NotFoundError):
                await instance.app.presets.save_version("nope", fixed_kwargs={"units": "v"})

    asyncio.run(run())
