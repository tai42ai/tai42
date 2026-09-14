"""Shared fakes + install helpers for the manifest-operation op-level oracles.

``_Admin`` / ``_install`` back the runtime ops (reload/deregister/list). ``_ReplaceStore``
+ ``_install_pipeline`` drive the replace pipeline (``update_manifest``); ``_MutateStore``
+ ``_install_mutate_pipeline`` drive the mutate pipeline (``set_mcp_config`` and the
per-entry / api_tools edits). ``_ReloadAdmin`` is the reload surface both pipelines call.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.app import instance

from ..._fakes.bus import FakeBus


class _Admin:
    def __init__(
        self,
        *,
        live_manifest: dict | None = None,
        results: dict[str, object] | None = None,
        raise_for: str | None = None,
    ) -> None:
        self.calls: list[tuple] = []
        self.live_manifest = live_manifest if live_manifest is not None else {"mcp": [{"title": "svc"}]}
        self._results = results or {}
        self._raise_for = raise_for

    def _dispatch(self, method: str, *args: object) -> object:
        self.calls.append((method, *args))
        if self._raise_for == method:
            raise RuntimeError(f"{method} failed")
        return self._results.get(method)

    def list_failed_mcps(self) -> object:
        return self._dispatch("list_failed_mcps")

    def reload_mcp(self, title: str) -> object:
        return self._dispatch("reload_mcp", title)

    def reload_failed_mcps(self) -> object:
        return self._dispatch("reload_failed_mcps")

    def deregister_mcp(self, title: str) -> object:
        return self._dispatch("deregister_mcp", title)


def _install(
    monkeypatch: pytest.MonkeyPatch, *, admin: _Admin, backend: object = None, bus: FakeBus | None = None
) -> FakeBus:
    impl = SimpleNamespace(admin=admin, backends=SimpleNamespace(backend=backend))
    monkeypatch.setattr(tai42_app, "_impl", impl)
    bus = bus or FakeBus()
    monkeypatch.setattr(instance.app, "_bus", bus)
    return bus


class _ReplaceStore:
    """A config manager whose ``replace_manifest`` records and persists the whole
    posted document — the seam the update_manifest pipeline drives."""

    def __init__(self, *, manifest: dict | None = None, env: dict | None = None) -> None:
        self.manifest: dict = manifest if manifest is not None else {}
        self.env: dict = env if env is not None else {}
        self.replaced: list[dict] = []

    def replace_manifest(self, document: dict) -> dict:
        self.replaced.append(dict(document))
        self.manifest = dict(document)
        return dict(document)

    def read_manifest_preserved(self) -> dict:
        return dict(self.manifest)

    def read_env(self) -> dict:
        return dict(self.env)


class _MutateStore:
    """A config manager whose ``mutate_manifest`` runs the guarded mutator on a copy of
    the stored document and persists only if it returns without raising — the seam the
    set_mcp_config pipeline drives (a raise inside leaves the store untouched)."""

    def __init__(self, *, manifest: dict | None = None) -> None:
        self.manifest: dict = manifest if manifest is not None else {}
        self.persisted: list[dict] = []

    def mutate_manifest(self, mutator: Any) -> dict:
        document = deepcopy(self.manifest)
        mutator(document)  # a raise here propagates before any persist
        self.manifest = document
        self.persisted.append(deepcopy(document))
        return document

    def read_manifest_preserved(self) -> dict:
        return deepcopy(self.manifest)


class _ReloadAdmin:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result if result is not None else {"status": "ok", "env_keys": 0}
        self.calls = 0

    def reload_config(self) -> dict:
        self.calls += 1
        return self.result


def _install_pipeline(
    monkeypatch: pytest.MonkeyPatch, *, store: _ReplaceStore, admin: _ReloadAdmin, backend: object = None
) -> FakeBus:
    impl = SimpleNamespace(
        config=SimpleNamespace(config_manager=store),
        admin=admin,
        backends=SimpleNamespace(backend=backend),
    )
    monkeypatch.setattr(tai42_app, "_impl", impl)
    bus = FakeBus()
    monkeypatch.setattr(instance.app, "_bus", bus)
    return bus


def _install_mutate_pipeline(monkeypatch: pytest.MonkeyPatch, *, store: _MutateStore, admin: _ReloadAdmin) -> FakeBus:
    impl = SimpleNamespace(
        config=SimpleNamespace(config_manager=store),
        admin=admin,
        backends=SimpleNamespace(backend=None),
    )
    monkeypatch.setattr(tai42_app, "_impl", impl)
    bus = FakeBus()
    monkeypatch.setattr(instance.app, "_bus", bus)
    return bus
