"""The uninstall flow: convergent deregister, skip paths, and corrupt-row handling."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tai42_skeleton.marketplace.errors import (
    InstallStateError,
    LocalStateError,
)
from tai42_skeleton.marketplace.store import InstallRecord

from ._specs import make_spec
from .test_installer import (
    Harness,
)

# -- uninstall ---------------------------------------------------------------


async def test_uninstall_happy_path() -> None:
    h = Harness(manifest={"tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.store.preload(spec, version="1.0.0")
    result = await h.installer().uninstall("tai42/toolbox")
    order = [e for e in h.events if e in ("cm:write", "reload", "pip", "store:delete")]
    assert order == ["cm:write", "reload", "pip", "store:delete"]
    assert h.pip.calls[-1] == ["uninstall", "--yes", "tai42-toolbox"]
    assert result["uninstalled"] is True
    assert "tai42/toolbox" not in h.store.rows
    # Never touches the registry.
    assert h.registry.resolve_calls == []


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
    # are deregistered before the package is pip-uninstalled and the row is dropped.
    h = Harness(manifest={})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.store.preload(spec, version="1.0.0")
    result = await h.installer().uninstall("tai42/toolbox")
    # The apply re-persists the (unchanged) empty manifest and reloads through the pipeline.
    assert h.svc.writes == [{}]
    assert h.svc.calls == 1
    assert result["reload"]["reloaded"] is True
    # Convergence order: the apply (persist + deregister reload) BEFORE pip uninstall + row delete.
    order = [e for e in h.events if e in ("cm:write", "reload", "pip", "store:delete")]
    assert order == ["cm:write", "reload", "pip", "store:delete"]
    assert h.pip.calls[-1] == ["uninstall", "--yes", "tai42-toolbox"]


async def test_uninstall_env_selected_only_plugin_takes_skip_path() -> None:
    h = Harness(manifest={})
    spec = make_spec(provides=[{"kind": "config", "name": "vault", "module": "pkg.config.vault", "description": "d"}])
    h.store.preload(spec, version="1.0.0")
    result = await h.installer().uninstall("tai42/toolbox")
    assert result["reload"] is None
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
