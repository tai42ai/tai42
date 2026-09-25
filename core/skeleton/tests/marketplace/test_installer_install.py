"""The install flow: happy step order, pre-flight refusals, and the unwind matrix."""

from __future__ import annotations

import copy
import importlib.metadata
from typing import Any

import pytest

from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.marketplace import package_ops
from tai42_skeleton.marketplace.errors import (
    ArtifactIntegrityError,
    ContractIncompatibleError,
    InstallStateError,
    InstallUnwindError,
    MalformedRefError,
    ManifestCollisionError,
    ManifestComposeError,
    PipFailedError,
    RegistryResponseError,
    VersionRefusedError,
)
from tai42_skeleton.operations._broadcast import FleetBroadcastError

from ._specs import make_resolved, make_spec
from .test_installer import (
    _GH_ARTIFACT,
    Harness,
    _assert_no_pip,
    _fake_verified_fetch,
)

# -- install: happy ----------------------------------------------------------


async def test_install_happy_step_order_manifest_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.registry.resolved = make_resolved(spec, version="1.0.0")

    result = await h.installer().install("tai42/toolbox")  # no version -> resolved pin

    # Fleet lock wraps the whole operation; resolve/pip/write/reload/record order.
    assert h.events[0] == "lock:acquire"
    assert h.events[-1] == "lock:release"
    order = [e for e in h.events if e in ("registry:resolve", "pip", "cm:write", "reload", "store:record")]
    assert order == ["registry:resolve", "pip", "cm:write", "reload", "store:record"]
    # The persisted manifest carries the patched tool entry.
    assert h.svc.writes[-1]["tools"] == [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]
    # The response reports the RESOLVED version even though the caller passed None.
    assert result["version"] == "1.0.0"
    assert result["package"] == "tai42-toolbox"
    # The reload field carries the local reload result plus the standard fleet fan-out
    # summary (single-worker harness => the local-only note).
    assert result["reload"]["reloaded"] is True
    assert result["reload"]["fanout"] == {
        "mode": "local-only",
        "note": "no worker bus configured; only this worker reloaded",
    }


async def test_install_config_item_contributes_a_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    spec = make_spec(provides=[{"kind": "config", "name": "vault", "module": "pkg.config.vault", "description": "d"}])
    h.registry.resolved = make_resolved(spec)
    result = await h.installer().install("tai42/toolbox")
    assert any("TAI_CONFIG_MODE" in note for note in result["notes"])


async def test_install_version_pinning_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    spec = make_spec(version="1.2.3")
    h.registry.resolved = make_resolved(spec, version="1.2.3")
    await h.installer().install("tai42/toolbox", "1.2.3")
    assert h.registry.resolve_calls == [("tai42", "toolbox", "1.2.3")]
    assert h.pip.calls[0][-1] == "tai42-toolbox==1.2.3"


async def test_install_github_fetches_verifies_and_installs_local_tarball(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    calls = _fake_verified_fetch(monkeypatch)
    h = Harness()
    spec = make_spec(version="2.0.0")
    h.registry.resolved = make_resolved(
        spec,
        source="github",
        version="2.0.0",
        repository_url="https://github.com/tai42ai/toolbox",
        tag="v2.0.0",
        artifact_ref=_GH_ARTIFACT,
        sha256="a" * 64,
    )
    await h.installer().install("tai42/toolbox")

    # The verified fetch was driven with the registry artifact_ref + sha256.
    assert calls[0]["artifact_ref"] == _GH_ARTIFACT
    assert calls[0]["sha256"] == "a" * 64
    # pip installed the verified LOCAL tarball path, never a git+url clone.
    assert h.pip.calls[0][-1] == str(calls[0]["path"])
    assert not h.pip.calls[0][-1].startswith("git+")
    # The attribution row keeps the github provenance AND the verified pin, so a
    # later update-unwind can re-fetch and re-verify the old artifact.
    row = h.store.record_calls[-1]
    assert row[3:7] == ("https://github.com/tai42ai/toolbox", "v2.0.0", _GH_ARTIFACT, "a" * 64)


async def test_install_github_integrity_mismatch_never_calls_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    # A sha256 mismatch during the verified fetch aborts the install: no pip, no
    # manifest write, no attribution — and no git fallback.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")

    async def mismatch(package, version, artifact_ref, sha256, dest_dir):
        raise ArtifactIntegrityError(expected_sha256="a" * 64, actual_sha256="b" * 64, artifact_ref=artifact_ref)

    monkeypatch.setattr(package_ops, "fetch_verified_artifact", mismatch)
    h = Harness()
    spec = make_spec(version="2.0.0")
    h.registry.resolved = make_resolved(
        spec, source="github", version="2.0.0", repository_url="https://github.com/tai42ai/toolbox", tag="v2.0.0"
    )
    with pytest.raises(ArtifactIntegrityError):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)
    assert h.svc.writes == []
    assert h.store.record_calls == []


async def test_install_github_fetch_failure_never_calls_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    # A download failure likewise aborts with no pip and no git fallback.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")

    async def boom(package, version, artifact_ref, sha256, dest_dir):
        raise RuntimeError("network down")

    monkeypatch.setattr(package_ops, "fetch_verified_artifact", boom)
    h = Harness()
    spec = make_spec(version="2.0.0")
    h.registry.resolved = make_resolved(
        spec, source="github", version="2.0.0", repository_url="https://github.com/tai42ai/toolbox", tag="v2.0.0"
    )
    with pytest.raises(RuntimeError, match="network down"):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


# -- install: pre-flight refusals (pip never called) -------------------------


async def test_install_already_installed_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness()
    spec = make_spec()
    h.store.preload(spec, version="1.0.0")
    with pytest.raises(InstallStateError, match="already installed"):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


async def test_install_no_published_version_maps_to_not_found() -> None:
    from tai42_skeleton.marketplace.errors import ListingNotFoundError

    h = Harness()
    h.registry.resolve_error = ListingNotFoundError("marketplace listing not found: tai42/toolbox")
    with pytest.raises(ListingNotFoundError):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


async def test_install_killed_version_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness()
    h.registry.resolve_error = VersionRefusedError("version is killed")
    with pytest.raises(VersionRefusedError):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


async def test_install_critical_advisory_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    spec = make_spec()
    h.registry.resolved = make_resolved(
        spec, advisories=[{"severity": "critical", "withdrawn_at": None, "summary": "RCE"}]
    )
    with pytest.raises(VersionRefusedError, match="critical advisory"):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


async def test_install_noncritical_advisory_installs_and_rides_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    spec = make_spec()
    adv = [{"severity": "medium", "withdrawn_at": None, "summary": "minor"}]
    h.registry.resolved = make_resolved(spec, advisories=adv)
    result = await h.installer().install("tai42/toolbox")
    assert h.pip.calls  # installed
    assert result["advisories"] == adv


async def test_install_invalid_spec_is_registry_response_error(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness()
    spec = make_spec()
    resolved = make_resolved(spec)
    resolved["spec"] = {"not": "a valid spec"}
    h.registry.resolved = resolved
    with pytest.raises(RegistryResponseError, match="invalid plugin spec"):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


async def test_install_contract_incompatible_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "2.0.0")
    h = Harness()
    spec = make_spec()
    h.registry.resolved = make_resolved(spec, contract_range=">=0.1,<1.0")
    with pytest.raises(ContractIncompatibleError) as exc:
        await h.installer().install("tai42/toolbox")
    assert ">=0.1,<1.0" in str(exc.value)
    assert "2.0.0" in str(exc.value)
    _assert_no_pip(h)


async def test_install_dev_versioned_contract_inside_range_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dev/pre-release installed contract inside the range must NOT be refused
    # (the SpecifierSet is evaluated with prereleases=True).
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.5.0.dev3")
    h = Harness()
    spec = make_spec()
    h.registry.resolved = make_resolved(spec, contract_range=">=0.1,<1.0")
    await h.installer().install("tai42/toolbox")
    assert h.pip.calls  # proceeded to install


async def test_install_manifest_collision_preflight_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness(manifest={"tools": [{"title": "x", "module": "pkg.tools.uuid"}]})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(ManifestCollisionError):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


async def test_install_bad_ref_raises_malformed_ref_error() -> None:
    h = Harness()
    # A malformed ref is a typed MalformedRefError (mapped to a 400 at the
    # boundary), distinct from a server-side invariant fault.
    with pytest.raises(MalformedRefError, match="namespace/name"):
        await h.installer().install("noslash")


# -- install: unwind matrix --------------------------------------------------


async def test_install_pip_failure_leaves_manifest_and_store_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    h.pip.fail_on = {0}
    spec = make_spec()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(PipFailedError):
        await h.installer().install("tai42/toolbox")
    assert h.svc.writes == []
    assert h.store.record_calls == []


async def test_install_manifest_persist_failure_unwinds_with_pip_uninstall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    # The manifest persist fails BEFORE the change lands (nothing persisted), so the
    # pipeline aborts and the installer needs no manifest restore — just the pip
    # uninstall of the freshly-installed package.
    h.svc.fail_persist_on = {0}
    spec = make_spec()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(RuntimeError, match="manifest persist failed"):
        await h.installer().install("tai42/toolbox")
    # pip install then the unwind pip uninstall; nothing persisted, so no restore.
    assert h.pip.calls[0][0] == "install"
    assert h.pip.calls[1] == ["uninstall", "--yes", "tai42-toolbox"]
    assert h.svc.writes == []


async def test_install_reload_failure_restores_manifest_and_uninstalls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    # The apply persists but its local reload fails afterwards (FleetBroadcastError):
    # the change landed, so the unwind must restore the manifest through the pipeline.
    h.svc.fail_reload_on = {0}
    spec = make_spec()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(FleetBroadcastError, match="reload failed"):
        await h.installer().install("tai42/toolbox")
    # Manifest patched then restored (the saved pre-patch dict = {}).
    assert h.svc.writes[-1] == {}
    # Two applies (forward + converge-back), pip install then uninstall.
    assert h.svc.calls == 2
    assert h.pip.calls[-1] == ["uninstall", "--yes", "tai42-toolbox"]


async def test_install_broadcast_failure_after_persist_restores_manifest_and_uninstalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    # The apply persists and the local reload succeeds, but the FLEET BROADCAST then
    # raises. ConfigService wraps that raw broadcast fault as a FleetBroadcastError with
    # the bus-unreachable report — so the change LANDED and the installer's persist
    # detector (persisted=True on a FleetBroadcastError) MUST restore the manifest. This
    # is the exact half-write the fix closes: without the wrap the raw error would read
    # persisted=False, skip the restore, yet still pip-uninstall the package.
    h.svc.fail_broadcast_on = {0}
    spec = make_spec()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(FleetBroadcastError, match="broadcast failed"):
        await h.installer().install("tai42/toolbox")
    # Manifest patched then RESTORED to the saved pre-patch dict ({}): no half-write.
    assert h.svc.writes[-1] == {}
    # Two applies (forward patch + converge-back restore), and the freshly-installed
    # package is pip-uninstalled — the manifest never references a pip-removed package.
    assert h.svc.calls == 2
    assert h.pip.calls[-1] == ["uninstall", "--yes", "tai42-toolbox"]


async def test_install_store_failure_full_unwind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    h.store.record_error = RuntimeError("pg down")
    spec = make_spec()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(RuntimeError, match="pg down"):
        await h.installer().install("tai42/toolbox")
    assert h.svc.writes[-1] == {}  # restored
    assert h.svc.calls == 2  # restore apply (converge-back)
    assert h.pip.calls[-1] == ["uninstall", "--yes", "tai42-toolbox"]


async def test_install_unwind_substep_failure_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    h.svc.fail_reload_on = {0}  # forward apply persists then its reload fails
    h.pip.fail_on = {1}  # the unwind pip uninstall also fails
    spec = make_spec()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(InstallUnwindError) as exc:
        await h.installer().install("tai42/toolbox")
    # FleetBroadcastError is a RuntimeError; the restore apply succeeded, so the
    # escalating sub-step is the failing pip uninstall.
    assert isinstance(exc.value.step_error, FleetBroadcastError)
    assert isinstance(exc.value.unwind_error, PipFailedError)


async def test_install_compose_failure_is_manifest_compose_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A composed manifest whose RESOLVED projection fails the schema is a registry-spec
    # + local-manifest fault: the pipeline raises inside the transaction (nothing
    # persists) and the installer maps that ValidationError to the typed compose error
    # (a 500), then unwinds the pip install. Here a pre-existing api_tools entry carries
    # a non-bool literal, so the resolved compose fails Manifest.model_validate.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness(manifest={"api_tools": {"expose_destructive": "not-a-bool"}})
    spec = make_spec()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(ManifestComposeError):
        await h.installer().install("tai42/toolbox")
    # Nothing persisted; the freshly-installed package is unwound.
    assert h.svc.writes == []
    assert h.pip.calls[0][0] == "install"
    assert h.pip.calls[-1] == ["uninstall", "--yes", "tai42-toolbox"]


async def test_install_env_marker_on_non_string_field_validates_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    # A composed manifest that sets a NON-STRING field via !ENV must validate against
    # the RESOLVED value, not the literal marker string. The pipeline materializes the
    # marker before Manifest.model_validate, so the install succeeds and the preserved
    # !ENV marker persists verbatim — the resolved bool never bakes in. Validating the
    # PRESERVED document instead would reject "!ENV ${EXPOSE_DESTRUCTIVE}" as an invalid
    # bool and 500 the install even though the resolved manifest is valid.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    monkeypatch.setenv("EXPOSE_DESTRUCTIVE", "true")
    marker = "!ENV ${EXPOSE_DESTRUCTIVE}"
    h = Harness(manifest={"api_tools": {"expose_destructive": marker}})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.registry.resolved = make_resolved(spec, version="1.0.0")

    result = await h.installer().install("tai42/toolbox")

    # The resolved expose_destructive=true is a valid bool, so the install succeeded; the
    # persisted manifest carries both the patched tool AND the untouched !ENV marker.
    persisted = h.svc.writes[-1]
    assert persisted["tools"] == [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]
    assert persisted["api_tools"]["expose_destructive"] == marker
    # One apply persisted + reloaded + broadcast, and the attribution row was written.
    assert h.svc.calls == 1
    assert result["reload"]["reloaded"] is True
    assert h.store.record_calls[-1][0] == "tai42/toolbox"


async def test_install_backend_needs_bus_maps_to_manifest_compose_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A plugin that registers a backend on a busless deployment fails the pipeline's
    # backend-needs-bus invariant. The marketplace surface translates only its own error
    # family, so the installer maps that refusal to the typed compose error — a loud,
    # attributed 500 carrying the "Set TAI_BUS_REDIS_URL" message — rather than letting
    # the RuntimeError escape untyped. Nothing persists; the pip install unwinds.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    h.svc.raise_on_validate = BackendNeedsBusError(
        "Refusing a config that registers a task backend ('pkg.backend') with no worker bus. Set TAI_BUS_REDIS_URL."
    )
    spec = make_spec(provides=[{"kind": "backend", "name": "worker", "module": "pkg.backend", "description": "d"}])
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(ManifestComposeError, match="TAI_BUS_REDIS_URL"):
        await h.installer().install("tai42/toolbox")
    assert h.svc.writes == []
    assert h.pip.calls[-1] == ["uninstall", "--yes", "tai42-toolbox"]


# -- install: unwind reload-back escalation ----------------------------------


async def test_install_unwind_reload_back_failure_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    # The forward apply (call 0) persists then its reload fails, triggering the
    # unwind; the unwind's restore apply (call 1) then fails its reload too. That
    # failed sub-step escalates to InstallUnwindError before the pip uninstall is
    # ever attempted.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    h.svc.fail_reload_on = {0, 1}  # forward apply AND the unwind restore apply both fail their reload
    spec = make_spec()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(InstallUnwindError) as exc:
        await h.installer().install("tai42/toolbox")
    assert isinstance(exc.value.step_error, FleetBroadcastError)
    assert isinstance(exc.value.unwind_error, FleetBroadcastError)
    # The manifest restore apply failed, so the pip uninstall was never reached.
    assert [c[0] for c in h.pip.calls] == ["install"]


# -- install: forward-reload route-collision recovery ------------------------


def _collision_forward(monkeypatch: pytest.MonkeyPatch):
    """Stub the forward provides reload to fail with a cross-owner route collision.

    Mirrors production: the manifest persisted, but the reload mounted the remapped item at
    its declared base, so it raises ``FleetBroadcastError`` whose ``__cause__`` is a
    ``CrossOwnerRouteCollisionError`` (``raise ... from`` sets the cause, as ConfigService does)."""
    from tai42_skeleton.app.bus.models import FleetResult
    from tai42_skeleton.app.route_registry import CrossOwnerRouteCollisionError
    from tai42_skeleton.marketplace import env_apply

    collision = CrossOwnerRouteCollisionError(
        "route GET /api/x (owner a) collides with GET /api/x (owner b) — "
        "one owner per route shape; remap the mount base to resolve"
    )

    async def _forward(*_args, **_kwargs):
        raise FleetBroadcastError(
            "reload_config", FleetResult(op="reload_config", reachable=True), collision
        ) from collision

    monkeypatch.setattr(env_apply, "apply_provides_change", _forward)


def _route(base: str) -> Any:
    from tai42_skeleton.marketplace.routes import ResolvedRoute

    return ResolvedRoute(
        item="r",
        kind="router",
        base=base,
        default_base="clash",
        path="/x",
        full_path=f"/api/{base}/x",
        methods=("GET",),
        public=True,
    )


async def test_commit_install_recovers_a_remapped_route_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    # The forward reload collides at the declared base; the recovery records the row (with the
    # resolved mounts) and remounts authoritatively via apply_replace at the resolved base.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    inst = h.installer()
    spec = make_spec()
    resolved = make_resolved(spec, version="1.0.0")
    saved = copy.deepcopy(h.cm.read_manifest_preserved())
    _collision_forward(monkeypatch)

    result = await inst._commit_install(
        "tai42/toolbox", "1.0.0", spec, "pypi", resolved, {"r": "remap"}, [_route("remap")], None, None, saved
    )

    # The row was recorded with the resolved mounts, and the result is the remount's apply.
    assert h.store.record_calls[-1][0] == "tai42/toolbox"
    assert h.store.record_calls[-1][10] == {"r": "remap"}
    assert result.local == {"reloaded": True}
    assert "store:delete" not in h.events


async def test_commit_install_unwinds_when_the_recovery_remount_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the recovery remount itself fails, the row is dropped, the manifest is unwound, and
    # the error re-raised — no silent half-install.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    inst = h.installer()
    spec = make_spec()
    resolved = make_resolved(spec, version="1.0.0")
    saved = copy.deepcopy(h.cm.read_manifest_preserved())
    _collision_forward(monkeypatch)

    # apply_replace raises on the recovery remount (call 1); the unwind's restore (call 2) works.
    original = h.svc.apply_replace
    calls = {"n": 0}

    async def _replace(document: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("remount build failed")
        return await original(document)

    monkeypatch.setattr(h.svc, "apply_replace", _replace)

    with pytest.raises(RuntimeError, match="remount build failed"):
        await inst._commit_install(
            "tai42/toolbox", "1.0.0", spec, "pypi", resolved, {"r": "remap"}, [_route("remap")], None, None, saved
        )

    assert "store:delete" in h.events  # the row recorded before the failed remount is dropped
    assert h.store.rows == {}
