"""Shared fixtures + helpers for the op-level preset oracles.

The op-level oracles pin the op-only branches the route round-trips do not reach —
the pure body-structure readers, the residual / typed-race error paths inside the
mutating ops, the reference graph, and the destructive projection. Each sub-surface
has its own module beside this file; the fixtures they all share live here.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import presets as preset_ops

from ...versioning.conftest import FakeVersioningPg

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [
        {
            "title": "fx",
            "module": "tests.presets._fixtures",
            "include": ["weather", "echo", "plan_tool", "boom_tool", "secret_sink"],
        }
    ],
    "agents": [
        {
            "title": "ag",
            "module": "tests.routers._authoring_fixtures",
            "include": ["authorable_agent", "locked_agent"],
        }
    ],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


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
    """Tear down every runtime-registered / quarantined preset after each test —
    the singleton ``PresetManager`` outlives one ``app_context``."""
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


async def _create(name: str, base_tool: str = "weather", **over: Any) -> None:
    # ``description`` is required non-empty on create, so the helper seeds a default
    # one; a test that exercises the description gate overrides it.
    await preset_ops.create_preset(
        name=name,
        base_tool=base_tool,
        description=over.get("description", "d"),
        fixed_kwargs=over.get("fixed_kwargs", {}),
        extensions=over.get("extensions", []),
        output_schema=over.get("output_schema"),
    )
