"""The uninstall flow: convergent deregister, skip paths, and corrupt-row handling."""

from __future__ import annotations

import importlib.metadata
from datetime import UTC, datetime

import pytest

from tai42_skeleton.marketplace.errors import (
    InstallStateError,
    LocalStateError,
)
from tai42_skeleton.marketplace.manifest_apply import converge_at_next_start
from tai42_skeleton.marketplace.store import InstallRecord
from tai42_skeleton.marketplace.unwind import ENVIRONMENT_RESTORED

from ._specs import make_resolved, make_spec
from .test_installer import (
    Harness,
    layer_inputs,
)

# -- uninstall ---------------------------------------------------------------


async def test_uninstall_happy_path() -> None:
    h = Harness(manifest={"tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.store.preload(spec, version="1.0.0")
    result = await h.installer().uninstall("tai42/toolbox")
    order = [e for e in h.events if e in ("cm:write", "reload", "pip", "store:delete")]
    # The layer is rebuilt without the plugin: with no other in-process plugin it is the
    # empty layer, built with no pip call, and swapped in before the deregister.
    assert order == ["cm:write", "reload", "store:delete"]
    assert layer_inputs(h) == []
    assert result["uninstalled"] is True
    assert result["manifest_written"] is True
    assert "tai42/toolbox" not in h.store.rows
    # Never touches the registry.
    assert h.registry.resolve_calls == []


async def test_uninstall_at_next_start_writes_the_manifest_and_runs_no_reload() -> None:
    h = Harness(manifest={"tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.store.preload(spec, version="1.0.0")

    result = await h.installer(converge=converge_at_next_start).uninstall("tai42/toolbox")

    order = [e for e in h.events if e in ("cm:write", "reload", "pip", "store:delete")]
    assert order == ["cm:write", "store:delete"]
    assert h.cm._manifest.get("tools", []) == []
    assert result["manifest_written"] is True
    assert result["reload"] is None
    assert result["uninstalled"] is True
    assert "tai42/toolbox" not in h.store.rows
    assert layer_inputs(h) == []


async def test_uninstall_at_next_start_env_selected_only_plugin_writes_nothing() -> None:
    h = Harness(manifest={})
    spec = make_spec(provides=[{"kind": "config", "name": "vault", "module": "pkg.config.vault", "description": "d"}])
    h.store.preload(spec, version="1.0.0")

    result = await h.installer(converge=converge_at_next_start).uninstall("tai42/toolbox")

    assert "cm:write" not in h.events
    assert result["manifest_written"] is False
    assert result["reload"] is None
    assert "tai42/toolbox" not in h.store.rows


async def test_uninstall_at_next_start_whose_persist_fails_swaps_the_environment_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness(manifest={})
    h.registry.resolved = make_resolved(
        make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    )
    await h.installer().install("tai42/toolbox")
    assert layer_inputs(h) == ["tai42/toolbox 1.0.0"]
    h.svc.fail_persist_on = {h.svc.calls}

    with pytest.raises(RuntimeError, match="manifest persist failed") as caught:
        await h.installer(converge=converge_at_next_start).uninstall("tai42/toolbox")

    assert ENVIRONMENT_RESTORED in (caught.value.__notes__ or [])
    assert layer_inputs(h) == ["tai42/toolbox 1.0.0"]
    assert "tai42/toolbox" in h.store.rows


async def test_uninstall_unknown_ref_is_not_installed() -> None:
    h = Harness()
    with pytest.raises(InstallStateError) as exc:
        await h.installer().uninstall("tai42/gone")
    assert exc.value.not_installed is True


async def test_uninstall_reloads_to_convergence_when_manifest_already_clean() -> None:
    # A row exists but the manifest has no matching entry (a re-run after a partial
    # uninstall whose deregister reload failed): the spec still targets manifest
    # fields, so the pipeline apply is RE-ATTEMPTED — an idempotent re-strip that
    # re-persists the (already clean) manifest and reloads so the still-live tools
    # are deregistered against the layer rebuilt without the package, then the row is dropped.
    h = Harness(manifest={})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.store.preload(spec, version="1.0.0")
    result = await h.installer().uninstall("tai42/toolbox")
    # The apply re-persists the (unchanged) empty manifest and reloads through the pipeline.
    assert h.svc.writes == [{}]
    assert h.svc.calls == 1
    assert result["reload"].local["reloaded"] is True
    # Convergence order: the apply (persist + deregister reload) BEFORE the row delete.
    order = [e for e in h.events if e in ("cm:write", "reload", "store:delete")]
    assert order == ["cm:write", "reload", "store:delete"]
    assert layer_inputs(h) == []


async def test_uninstall_rebuilds_the_layer_from_the_remaining_in_process_plugins() -> None:
    h = Harness(manifest={"tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    other = make_spec(
        name="other",
        package="tai42-other",
        provides=[{"kind": "tool", "name": "other-tool", "module": "other.tools", "description": "d"}],
    )
    h.store.preload(spec, version="1.0.0")
    h.store.preload(other, version="2.0.0")

    await h.installer().uninstall("tai42/toolbox")

    # One pip call builds the layer from what stays installed; the uninstalled package is
    # never named, so nothing is uninstalled from a running environment.
    assert len(h.pip.calls) == 1
    assert "tai42-other==2.0.0" in h.pip.calls[0]
    assert not any("tai42-toolbox" in arg for arg in h.pip.calls[0])
    assert layer_inputs(h) == ["tai42/other 2.0.0"]


async def test_an_uninstall_whose_row_delete_fails_swaps_the_environment_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness(manifest={})
    h.registry.resolved = make_resolved(make_spec())
    await h.installer().install("tai42/toolbox")
    assert layer_inputs(h) == ["tai42/toolbox 1.0.0"]
    h.store.delete_error = RuntimeError("pg down")

    with pytest.raises(RuntimeError, match="pg down") as caught:
        await h.installer().uninstall("tai42/toolbox")

    assert ENVIRONMENT_RESTORED in (caught.value.__notes__ or [])
    assert layer_inputs(h) == ["tai42/toolbox 1.0.0"]
    assert "tai42/toolbox" in h.store.rows


async def test_uninstall_env_selected_only_plugin_takes_skip_path() -> None:
    h = Harness(manifest={})
    spec = make_spec(provides=[{"kind": "config", "name": "vault", "module": "pkg.config.vault", "description": "d"}])
    h.store.preload(spec, version="1.0.0")
    result = await h.installer().uninstall("tai42/toolbox")
    assert result["reload"] is None
    assert result["manifest_written"] is False
    # The config provider gets a loud note about TAI_CONFIG_MODE.
    assert any("TAI_CONFIG_MODE" in note for note in result["notes"])


async def test_uninstall_corrupt_local_row_is_local_state_error() -> None:
    h = Harness()
    # A stored spec that no longer validates is corrupt LOCAL state -> 500, not 400.
    h.store.rows["tai42/toolbox"] = InstallRecord(
        ref="tai42/toolbox",
        version="1.0.0",
        source="pypi",
        repository_url=None,
        tag=None,
        spec={"garbage": True},
        installed_at=datetime.now(UTC),
    )
    with pytest.raises(LocalStateError):
        await h.installer().uninstall("tai42/toolbox")
