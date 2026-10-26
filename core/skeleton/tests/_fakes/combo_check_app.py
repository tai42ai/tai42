"""A booted app carrying the dry-run combo check's fixture extensions over the bound base ``echo``.

The manifest binds tool ``echo`` of :mod:`tests.app._fixtures.tools_check` under the ``tools``
entry ``a`` and loads the extensions of :mod:`tests.app._fixtures.ext_check`. The boot imports the
extension module afresh, so its recorded factory calls are read through ``sys.modules``.
"""

from __future__ import annotations

import asyncio
import copy
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from pyaml_env import parse_config
from tai42_kit.utils.data import dump_manifest

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding.surface import remove_surface_tool

from .write_span import ExclusiveWriteStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

EXTENSIONS_MODULE = "tests.app._fixtures.ext_check"
TOOLS_MODULE = "tests.app._fixtures.tools_check"

CHECK_DOCUMENT: dict[str, Any] = {
    "extensions_modules": [EXTENSIONS_MODULE],
    "tools": [{"title": "a", "module": TOOLS_MODULE, "include": ["echo"]}],
}


def factory_calls() -> list[str]:
    """The factory calls the booted extension module recorded, by extension name."""
    return sys.modules[EXTENSIONS_MODULE].CALLS


@asynccontextmanager
async def booted_check_app() -> AsyncIterator[None]:
    """Serve :data:`CHECK_DOCUMENT` for the duration of the block."""
    async with app.app_context(Manifest.model_validate(CHECK_DOCUMENT)):
        yield


class LiveStore(ExclusiveWriteStore):
    """The live config manager's stored manifest and env, held in memory.

    Patched onto ``app.config.config_manager`` so every door's pipeline reads and writes it;
    ``mutate_manifest`` persists only when the mutator returns without raising, as the real
    transaction does. ``read_manifest`` is the runtime view, ``!ENV`` markers materialized
    against the process env; ``read_manifest_preserved`` keeps the markers.
    """

    def __init__(self) -> None:
        super().__init__()
        self.manifest: dict[str, Any] = copy.deepcopy(CHECK_DOCUMENT)
        self.env: dict[str, str] = {}
        self.manifest_writes = 0
        self.env_writes = 0

    def read_manifest(self) -> dict[str, Any]:
        return parse_config(data=dump_manifest(cast("Any", copy.deepcopy(self.manifest)))) or {}

    def read_manifest_preserved(self) -> dict[str, Any]:
        return copy.deepcopy(self.manifest)

    def mutate_manifest(self, mutator: Any) -> dict[str, Any]:
        document = copy.deepcopy(self.manifest)
        mutator(document)
        self.manifest = document
        self.manifest_writes += 1
        return copy.deepcopy(document)

    def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]:
        self.manifest = copy.deepcopy(document)
        self.manifest_writes += 1
        return copy.deepcopy(document)

    def read_env(self) -> dict[str, str]:
        return dict(self.env)

    def write_env(self, config: dict[str, str]) -> None:
        self.env = {key: value for key, value in {**self.env, **config}.items() if value != ""}
        self.env_writes += 1

    def replace_env(self, config: dict[str, str]) -> None:
        self.env = {key: value for key, value in config.items() if value != ""}
        self.env_writes += 1


@pytest.fixture
def live_store(monkeypatch: pytest.MonkeyPatch) -> LiveStore:
    """A :class:`LiveStore` wired in place of the live config manager's stored manifest and env."""
    store = LiveStore()
    manager = app.config.config_manager
    monkeypatch.setattr(manager, "read_manifest", store.read_manifest)
    monkeypatch.setattr(manager, "read_manifest_preserved", store.read_manifest_preserved)
    monkeypatch.setattr(manager, "mutate_manifest", store.mutate_manifest)
    monkeypatch.setattr(manager, "replace_manifest", store.replace_manifest)
    monkeypatch.setattr(manager, "read_env", store.read_env)
    monkeypatch.setattr(manager, "write_env", store.write_env)
    monkeypatch.setattr(manager, "replace_env", store.replace_env)
    monkeypatch.setattr(manager, "exclusive_write", store.exclusive_write)
    return store


@pytest.fixture
def clean_server() -> Iterator[None]:
    """Clear the singleton FastMCP server's tools around a test."""

    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            remove_surface_tool(app._fast_mcp, tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())
