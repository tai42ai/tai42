"""Shared recording fakes for the installer's step/unwind tests.

Every collaborator is a recording fake (registry, pip runner, attribution store,
config-mutation pipeline, fleet lock, config manager), driven deterministically by
a shared event log. The per-flow tests import these fakes from here; the
``installer`` module alias is re-exported for the sibling test modules that patch
symbols on it."""

from __future__ import annotations

import copy
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pyaml_env import parse_config
from tai42_contract.plugins import PluginSpec
from tai42_kit.utils.data import dump_manifest

from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.config.service import ApplyResult
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace import installer as installer_module
from tai42_skeleton.marketplace import package_ops
from tai42_skeleton.marketplace.errors import (
    OperationInProgressError,
    PipFailedError,
)
from tai42_skeleton.marketplace.installer import Installer
from tai42_skeleton.marketplace.store import InstallRecord
from tai42_skeleton.operations._broadcast import FleetBroadcastError

# Re-exported for the per-flow test modules and the sibling modules that patch
# symbols on the ``installer`` alias.
__all__ = [
    "_GH_ARTIFACT",
    "_GH_ARTIFACT_OLD",
    "FakeCM",
    "FakeConfigService",
    "FakeFleetLock",
    "FakePip",
    "FakeRegistry",
    "FakeStore",
    "Harness",
    "_assert_no_pip",
    "installer_module",
    "package_ops",
]


def _tool_provides(module: str) -> list[dict[str, str]]:
    return [{"kind": "tool", "name": "gen-uuid", "module": module, "description": "d"}]


_GH_ARTIFACT = "https://codeload.github.com/tai42ai/toolbox/tar.gz/refs/tags/v2.0.0"
_GH_ARTIFACT_OLD = "https://codeload.github.com/tai42ai/toolbox/tar.gz/refs/tags/v1.0.0"


def _assert_no_pip(h: Harness) -> None:
    assert h.pip.calls == []


class FakeRegistry:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.resolved: dict[str, Any] | None = None
        self.resolve_error: Exception | None = None
        self.resolve_calls: list[tuple[str, str, str | None]] = []
        # ref -> version rows (or an Exception to raise) for the upgrade-all probe.
        self.versions_map: dict[str, Any] = {}

    async def resolve(self, ns: str, name: str, version: str | None = None) -> dict[str, Any]:
        self._events.append("registry:resolve")
        self.resolve_calls.append((ns, name, version))
        if self.resolve_error is not None:
            raise self.resolve_error
        assert self.resolved is not None
        return self.resolved

    async def versions(self, ns: str, name: str) -> list[dict[str, Any]]:
        self._events.append("registry:versions")
        result = self.versions_map[f"{ns}/{name}"]
        if isinstance(result, Exception):
            raise result
        return result


class FakePip:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.calls: list[list[str]] = []
        self.fail_on: set[int] = set()

    async def __call__(self, args: list[str]) -> str:
        idx = len(self.calls)
        self.calls.append(args)
        self._events.append("pip")
        if idx in self.fail_on:
            raise PipFailedError(args, 1, "pip boom")
        return "pip ok"


class FakeStore:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.rows: dict[str, InstallRecord] = {}
        self.record_calls: list[tuple] = []
        self.record_error: Exception | None = None

    async def get(self, ref: str) -> InstallRecord | None:
        self._events.append("store:get")
        return self.rows.get(ref)

    async def record(
        self,
        ref,
        version,
        source,
        repository_url,
        tag,
        artifact_ref,
        sha256,
        spec,
        *,
        contract_version,
        skeleton_version,
        route_mounts=None,
    ) -> None:
        self._events.append("store:record")
        self.record_calls.append(
            (
                ref,
                version,
                source,
                repository_url,
                tag,
                artifact_ref,
                sha256,
                spec,
                contract_version,
                skeleton_version,
                route_mounts or {},
            )
        )
        if self.record_error is not None:
            raise self.record_error
        self.rows[ref] = InstallRecord(
            ref=ref,
            version=version,
            source=source,
            repository_url=repository_url,
            tag=tag,
            artifact_ref=artifact_ref,
            sha256=sha256,
            spec=spec,
            contract_version=contract_version,
            skeleton_version=skeleton_version,
            route_mounts=route_mounts or {},
            installed_at=datetime.now(UTC),
        )

    async def delete(self, ref: str) -> bool:
        self._events.append("store:delete")
        return self.rows.pop(ref, None) is not None

    async def list_installed(self) -> list[InstallRecord]:
        return list(self.rows.values())

    def preload(
        self,
        spec: PluginSpec,
        *,
        version: str,
        source: str = "pypi",
        repository_url=None,
        tag=None,
        artifact_ref=None,
        sha256=None,
    ) -> None:
        self.rows[spec.ref] = InstallRecord(
            ref=spec.ref,
            version=version,
            source=source,
            repository_url=repository_url,
            tag=tag,
            artifact_ref=artifact_ref,
            sha256=sha256,
            spec=spec.model_dump(mode="json"),
            installed_at=datetime.now(UTC),
        )


class FakeCM:
    """The read seam the installer drives for its pre-flights (collision check and
    the saved-manifest capture). Writes never go through here — they cross the
    :class:`FakeConfigService` pipeline, which mutates ``_manifest`` in place."""

    def __init__(self, events: list[str], manifest: dict[str, Any] | None = None) -> None:
        self._events = events
        self._manifest = manifest or {}

    def read_manifest(self) -> dict[str, Any]:
        self._events.append("cm:read")
        return copy.deepcopy(self._manifest)

    def read_manifest_preserved(self) -> dict[str, Any]:
        self._events.append("cm:read")
        return copy.deepcopy(self._manifest)


def _fleet_report() -> FleetResult:
    return FleetResult(op="reload_config", results=[])


def _unreachable_report() -> FleetResult:
    """The honest bus-unreachable report shape ConfigService now attaches when a
    broadcast raises after the persist committed — no origin list, only the error."""
    return FleetResult(op="reload_config", reachable=False, error="ResponseError: WRONGTYPE")


class FakeConfigService:
    """The manifest-mutation pipeline seam: records each apply and mutates the shared
    :class:`FakeCM`, standing in for :class:`ConfigService`.

    Like the real pipeline, ``apply_change`` validates the RESOLVED projection of the
    composed document BEFORE it persists — ``!ENV`` markers materialized through the
    same ``dump_manifest`` / ``parse_config`` round-trip — so a marker on a non-string
    manifest field validates against its resolved value, and a schema-invalid resolved
    compose raises a :class:`ValidationError` with nothing persisted. ``raise_on_validate``
    injects an invariant failure the pipeline would raise in that same gate (e.g. a
    backend-needs-bus refusal).

    ``fail_persist_on`` raises BEFORE the persist lands (a transaction/write failure
    — nothing persisted, no reload). ``fail_reload_on`` raises a
    :class:`FleetBroadcastError` AFTER the persist lands (the local reload failed once
    the change had committed and the broadcast went out). ``fail_broadcast_on`` models
    the third post-persist outcome: the persist committed and the local reload
    succeeded, but the FLEET BROADCAST then raised — ConfigService wraps that raw
    broadcast fault as a :class:`FleetBroadcastError` carrying the honest
    bus-unreachable report, so it too surfaces as a landed-but-propagation-failed
    change. Each apply appends ``cm:write`` then ``reload`` to the shared event log,
    mirroring the pipeline's persist-then-reload order, and increments ``calls``."""

    def __init__(self, events: list[str], cm: FakeCM) -> None:
        self._events = events
        self._cm = cm
        self.writes: list[dict[str, Any]] = []
        self.calls = 0
        self.fail_persist_on: set[int] = set()
        self.fail_reload_on: set[int] = set()
        self.fail_broadcast_on: set[int] = set()
        self.raise_on_validate: Exception | None = None

    async def apply_change(self, mutator: Any) -> ApplyResult:
        document = copy.deepcopy(self._cm._manifest)
        mutator(document)  # the structural provides patch (may raise, e.g. a collision)
        self._validate_resolved(document)  # the pipeline's pre-persist RESOLVED-projection gate
        return self._persist(document)

    def _validate_resolved(self, document: dict[str, Any]) -> None:
        # Materialize the ``!ENV`` markers exactly as ConfigService does, then validate
        # the schema — so a marker on a non-string field is checked against its resolved
        # value, never the literal marker string. ``raise_on_validate`` stands in for an
        # invariant the pipeline evaluates in this same gate (e.g. backend-needs-bus).
        if self.raise_on_validate is not None:
            raise self.raise_on_validate
        resolved = parse_config(data=dump_manifest(cast("Any", document))) or {}
        Manifest.model_validate(resolved)

    def _effective_env(self, changes: dict[str, str]) -> dict[str, str]:
        # The preview's missing-env computation reads the effective env through this
        # seam (stored env overlaid on the process env). The fake tracks no env store,
        # so the process env plus any ``changes`` stands in.
        return {**os.environ, **changes}

    async def apply_replace(self, document: dict[str, Any]) -> ApplyResult:
        return self._persist(copy.deepcopy(document))

    def _persist(self, document: dict[str, Any]) -> ApplyResult:
        idx = self.calls
        self.calls += 1
        if idx in self.fail_persist_on:
            raise RuntimeError(f"manifest persist failed at call {idx}")
        self._events.append("cm:write")
        self._cm._manifest = copy.deepcopy(document)
        self.writes.append(copy.deepcopy(document))
        self._events.append("reload")
        if idx in self.fail_reload_on:
            raise FleetBroadcastError("reload_config", _fleet_report(), RuntimeError(f"reload failed at call {idx}"))
        if idx in self.fail_broadcast_on:
            raise FleetBroadcastError(
                "reload_config", _unreachable_report(), RuntimeError(f"broadcast failed at call {idx}")
            )
        return ApplyResult(fleet=_fleet_report(), local={"reloaded": True}, document=document)


class FakeFleetLock:
    def __init__(self, events: list[str], *, held: bool = False) -> None:
        self._events = events
        self.held = held

    def __call__(self):
        return self._cm()

    @asynccontextmanager
    async def _cm(self):
        if self.held:
            raise OperationInProgressError("another marketplace operation is in progress; retry shortly")
        self._events.append("lock:acquire")
        try:
            yield
        finally:
            self._events.append("lock:release")


class Harness:
    def __init__(self, **cm_kwargs) -> None:
        self.events: list[str] = []
        self.registry = FakeRegistry(self.events)
        self.pip = FakePip(self.events)
        self.store = FakeStore(self.events)
        self.fleet = FakeFleetLock(self.events)
        self.cm = FakeCM(self.events, **cm_kwargs)
        self.svc = FakeConfigService(self.events, self.cm)

    def installer(self, *, owned_routes: Any = None, reserved_prefixes: Any = None) -> Installer:
        # The fakes duck-type the real collaborators; cast past the typed params.
        # ``owned_routes`` / ``reserved_prefixes`` default to the live seams; a route
        # test injects a deterministic set instead of the process registry/settings.
        extra: dict[str, Any] = {}
        if owned_routes is not None:
            extra["owned_routes"] = owned_routes
        if reserved_prefixes is not None:
            extra["reserved_prefixes"] = reserved_prefixes
        return Installer(
            registry=cast(Any, self.registry),
            pip_runner=self.pip,
            store=cast(Any, self.store),
            config_service=cast(Any, self.svc),
            fleet_lock=self.fleet,
            config_manager=self.cm,
            **extra,
        )


def _fake_verified_fetch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch the installer's ``fetch_verified_artifact`` to record its args and
    return the local tarball path it would have written, without a real download.
    Lets a test assert the installer fetched the registry artifact_ref + sha256
    and pip-installed the LOCAL tarball."""
    calls: list[dict[str, Any]] = []

    async def fake(package, version, artifact_ref, sha256, dest_dir):
        path = dest_dir / f"{package}-{version}.tar.gz"
        calls.append(
            {"package": package, "version": version, "artifact_ref": artifact_ref, "sha256": sha256, "path": path}
        )
        return path

    monkeypatch.setattr(package_ops, "fetch_verified_artifact", fake)
    return calls
