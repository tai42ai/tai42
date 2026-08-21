"""Descriptor-only (package-less) install/uninstall/update over faked seams.

A descriptor-only plugin (``spec.package is None`` — an all-connector listing) installs
NOTHING but its manifest ``connectors`` entry: no pip runs, no migrations, no package
removal. This pins:

* the pip runner is NEVER called for a descriptor spec on install / uninstall / update;
* an oauth connector's client-credential env lands in the store through the combined seam;
* the attribution row carries ``source='spec'`` + artifact_ref + sha256;
* a missing required connector env is refused BEFORE any write (the early pre-check);
* a ``spec`` source is accepted only with the full github-style pointer;
* a delivery-form change between versions (descriptor <-> package) is refused;
* a manifest-persist failure unwinds the manifest entry and the env keys this install wrote.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.config.service import OrphanEnvWriteError
from tai42_skeleton.marketplace.errors import InstallStateError, RegistryResponseError
from tai42_skeleton.marketplace.installer import Installer

from ._specs import connector_item, make_resolved, make_spec
from .test_installer_mcp_env import Harness, _FakeFleetLock


class _NeverPip:
    """A pip runner that fails loudly if invoked — a descriptor install must never pip."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        raise AssertionError(f"pip must not run for a descriptor-only spec, got: {args}")


def _connector_spec(*, provider_id: str = "acme", version: str = "1.0.0", kind: str = "oauth"):
    return make_spec(
        namespace="tai42",
        name="acme-connector",
        package=None,
        version=version,
        provides=[connector_item(provider_id, kind=kind)],
    )


def _spec_resolved(spec, *, version: str | None = None, **overrides):
    defaults: dict = {
        "source": "spec",
        "repository_url": "https://github.com/tai42ai/acme-connector",
        "tag": f"v{version or spec.version}",
        "artifact_ref": "https://raw.githubusercontent.com/tai42ai/acme-connector/v1/tai-plugin.yml",
        "sha256": "a" * 64,
    }
    defaults.update(overrides)
    return make_resolved(spec, version=version, **defaults)


def _descriptor_harness() -> tuple[Harness, _NeverPip]:
    h = Harness()
    pip = _NeverPip()
    h.pip = pip  # type: ignore[assignment]
    return h, pip


def _provider_dump(spec) -> dict:
    provider = spec.provides[0].provider
    assert provider is not None
    return provider.model_dump(mode="json", exclude_none=True)


def _installer(h: Harness) -> Installer:
    return Installer(
        registry=h.registry,  # type: ignore[arg-type]
        pip_runner=h.pip,  # type: ignore[arg-type]
        store=h.store,  # type: ignore[arg-type]
        config_service=h.svc,  # type: ignore[arg-type]
        fleet_lock=_FakeFleetLock(),
        config_manager=h.cm,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACME_CLIENT_ID", raising=False)
    monkeypatch.delenv("ACME_CLIENT_SECRET", raising=False)


async def test_descriptor_install_never_pips_and_lands_connector_and_env() -> None:
    spec = _connector_spec()
    h, pip = _descriptor_harness()
    h.registry.resolved = _spec_resolved(spec)
    result = await _installer(h).install(spec.ref, env={"ACME_CLIENT_ID": "id", "ACME_CLIENT_SECRET": "sec"})
    assert pip.calls == []  # no pip for a descriptor spec
    # The provider descriptor landed in the manifest connectors list.
    assert [c["id"] for c in h.cm._manifest["connectors"]] == ["acme"]
    # The client-credential env landed in the store.
    assert h.cm._env["ACME_CLIENT_ID"] == "id"
    assert h.cm._env["ACME_CLIENT_SECRET"] == "sec"
    # The receipt reports a descriptor delivery and no pip output.
    assert result["pip_output"] is None
    assert result["package"] is None
    # The attribution row carries the spec source + pointer.
    row = h.store.rows[spec.ref]
    assert row.source == "spec"
    assert row.artifact_ref == "https://raw.githubusercontent.com/tai42ai/acme-connector/v1/tai-plugin.yml"
    assert row.sha256 == "a" * 64


async def test_descriptor_install_missing_env_fails_before_any_write() -> None:
    spec = _connector_spec()
    h, pip = _descriptor_harness()
    h.registry.resolved = _spec_resolved(spec)
    # No env supplied and none in the process env -> the early pre-check refuses.
    with pytest.raises(InstallStateError, match="ACME_CLIENT_SECRET"):
        await _installer(h).install(spec.ref, env={"ACME_CLIENT_ID": "id"})
    assert pip.calls == []
    assert h.cm._env == {}
    assert h.cm._manifest.get("connectors", []) == []
    assert h.store.rows == {}


async def test_descriptor_uninstall_never_pips_and_drops_connector() -> None:
    spec = _connector_spec()
    h, pip = _descriptor_harness()
    # Seed the installed row + manifest entry.
    h.cm._manifest = {"connectors": [_provider_dump(spec)]}
    h.store.preload(spec)
    result = await _installer(h).uninstall(spec.ref)
    assert pip.calls == []
    assert result["uninstalled"] is True
    assert h.cm._manifest["connectors"] == []
    assert spec.ref not in h.store.rows


async def test_descriptor_update_never_pips() -> None:
    old = _connector_spec(version="1.0.0")
    new = _connector_spec(version="2.0.0")
    h, pip = _descriptor_harness()
    h.cm._manifest = {"connectors": [_provider_dump(old)]}
    h.cm._env = {"ACME_CLIENT_ID": "id", "ACME_CLIENT_SECRET": "sec"}
    h.store.preload(old)
    h.registry.resolved = _spec_resolved(new, version="2.0.0")
    result = await _installer(h).update(old.ref)
    assert pip.calls == []
    assert result["package"] is None
    assert h.store.rows[old.ref].version == "2.0.0"


async def test_spec_source_rejected_without_full_pointer() -> None:
    spec = _connector_spec()
    h, _pip = _descriptor_harness()
    # A spec source missing its repository_url/tag pointer is garbled registry data (502).
    h.registry.resolved = _spec_resolved(spec, repository_url=None, tag=None)
    with pytest.raises(RegistryResponseError, match="repository_url or tag"):
        await _installer(h).install(spec.ref, env={"ACME_CLIENT_ID": "id", "ACME_CLIENT_SECRET": "sec"})


async def test_delivery_form_change_between_versions_is_refused() -> None:
    # Installed as a descriptor; the new version ships a package (a packaged connector).
    old = _connector_spec(version="1.0.0")
    new = make_spec(
        namespace="tai42",
        name="acme-connector",
        package="tai42-acme-connector",
        version="2.0.0",
        provides=[connector_item("acme")],
    )
    h, pip = _descriptor_harness()
    h.store.preload(old)
    h.registry.resolved = make_resolved(new, version="2.0.0", source="github", repository_url="https://x", tag="v2.0.0")
    with pytest.raises(InstallStateError, match="delivery form changed between versions"):
        await _installer(h).update(old.ref)
    assert pip.calls == []
    assert h.store.rows[old.ref].version == "1.0.0"


async def test_descriptor_install_persist_failure_reverts_env_write() -> None:
    # Persist-FAILURE path: the combined pipeline writes the env FIRST, then the manifest
    # persist fails (OrphanEnvWriteError) with the connector NEVER persisted. The installer-
    # unwind reverts the env keys this install wrote — a genuine env-revert (the fake DID
    # write both keys before raising, so an empty store here proves the revert ran).
    spec = _connector_spec()
    h, pip = _descriptor_harness()
    h.registry.resolved = _spec_resolved(spec)
    h.svc.fail_persist = True
    with pytest.raises(OrphanEnvWriteError):
        await _installer(h).install(spec.ref, env={"ACME_CLIENT_ID": "id", "ACME_CLIENT_SECRET": "sec"})
    # The env write (which DID land in the store before the persist failure) is reverted, and
    # no attribution row was written.
    assert pip.calls == []
    assert h.cm._env == {}
    assert h.store.rows == {}


async def test_descriptor_install_attribution_failure_restores_saved_manifest() -> None:
    # Attribution-FAILURE path: the manifest persist SUCCEEDS (the connector is added and the
    # env lands), then a LATER step — the store.record attribution write — fails. The installer
    # unwind must RESTORE the saved manifest (dropping the just-added connector) and revert the
    # env, so the connector is genuinely added-THEN-removed (not never-added).
    spec = _connector_spec()
    h, pip = _descriptor_harness()
    h.registry.resolved = _spec_resolved(spec)

    captured: dict = {}

    async def _failing_record(*args: object, **kwargs: object) -> None:
        # Runs AFTER the manifest persist committed: snapshot the just-added connector + env,
        # then fail the attribution write to drive the saved-manifest restore.
        captured["connectors"] = [c["id"] for c in h.cm._manifest.get("connectors", [])]
        captured["env"] = dict(h.cm._env)
        raise RuntimeError("attribution store down")

    h.store.record = _failing_record  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="attribution store down"):
        await _installer(h).install(spec.ref, env={"ACME_CLIENT_ID": "id", "ACME_CLIENT_SECRET": "sec"})

    # The connector WAS added (and its env written) before the attribution step ran...
    assert captured["connectors"] == ["acme"]
    assert captured["env"]["ACME_CLIENT_ID"] == "id"
    assert captured["env"]["ACME_CLIENT_SECRET"] == "sec"
    # ...and the unwind restored the saved (empty) manifest, dropping the just-added connector,
    # and reverted the env write; no row remains.
    assert pip.calls == []
    assert h.cm._manifest.get("connectors", []) == []
    assert h.cm._env == {}
    assert h.store.rows == {}


async def test_descriptor_install_preview_reports_required_env_and_delivery() -> None:
    spec = _connector_spec()
    h, _pip = _descriptor_harness()
    h.registry.resolved = _spec_resolved(spec)
    preview = await _installer(h).preview(spec.ref)
    assert preview["delivery"] == "descriptor"
    names = {req["name"]: req["secret"] for req in preview["required_env"]}
    assert names == {"ACME_CLIENT_ID": False, "ACME_CLIENT_SECRET": True}
    # Neither var is present in the env store or process env -> both missing.
    assert set(preview["missing_env"]) == {"ACME_CLIENT_ID", "ACME_CLIENT_SECRET"}
