"""Install/uninstall into a configured plugin prefix, and the environment-shadow rule."""

from __future__ import annotations

from typing import Any, cast

import pytest

from tai42_skeleton.marketplace import installer as installer_module
from tai42_skeleton.marketplace.errors import (
    EnvironmentShadowError,
    PluginPrefixError,
)
from tai42_skeleton.marketplace.installer import Installer

from ._specs import make_resolved, make_spec
from .test_installer import (
    Harness,
    _assert_no_pip,
    _tool_provides,
)

# -- prefix: install into the persistent prefix, uninstall from it -----------


class FakePrefixUninstall:
    """Records the prefix removals the installer requests instead of shelling
    ``pip uninstall`` — the prefix path removes a plugin's own files directly."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.calls: list[tuple[str, str]] = []

    def __call__(self, package: str, prefix: str) -> None:
        self._events.append("prefix:uninstall")
        self.calls.append((package, prefix))


class FakeEnsureWritable:
    """Records the prefix write pre-flight the installer runs before an install."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.calls: list[str] = []

    def __call__(self, prefix: str) -> None:
        self._events.append("prefix:ensure_writable")
        self.calls.append(prefix)


_PREFIX = "/srv/tai42/plugins"


def _prefix_installer(
    h: Harness,
    remover: FakePrefixUninstall,
    writable: FakeEnsureWritable,
    *,
    env_version: str | None = None,
    prefix_has: bool = True,
) -> Installer:
    # ``env_version`` fakes what the running ENVIRONMENT provides for the target
    # distribution (None = absent); ``prefix_has`` fakes whether the distribution
    # is present under the prefix. Defaults (prefix-present, env-absent) model the
    # ordinary prefix install.
    return Installer(
        registry=cast(Any, h.registry),
        pip_runner=h.pip,
        store=cast(Any, h.store),
        config_service=cast(Any, h.svc),
        fleet_lock=h.fleet,
        config_manager=h.cm,
        prefix=_PREFIX,
        prefix_uninstall=remover,
        prefix_ensure_writable=writable,
        prefix_has_dist=lambda package, prefix: prefix_has,
        env_dist_version=lambda package, prefix: env_version,
    )


async def test_install_with_prefix_preflights_writable_and_targets_the_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer_module.importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.registry.resolved = make_resolved(spec, version="1.0.0")
    remover = FakePrefixUninstall(h.events)
    writable = FakeEnsureWritable(h.events)

    await _prefix_installer(h, remover, writable).install("tai42/toolbox")

    # The prefix writability is pre-flighted BEFORE any pip run — a set-but-broken
    # prefix fails loudly, never a silent fall back to the environment.
    assert writable.calls == [_PREFIX]
    assert h.events.index("prefix:ensure_writable") < h.events.index("pip")
    # pip install targets the prefix (image deps resolve against the environment).
    assert h.pip.calls[0][3:5] == ["--prefix", _PREFIX]
    assert h.pip.calls[0][-1] == "tai42-toolbox==1.0.0"
    # A happy install removes nothing.
    assert remover.calls == []


async def test_uninstall_with_prefix_removes_from_prefix_and_shells_no_pip() -> None:
    h = Harness(manifest={"tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.store.preload(spec, version="1.0.0")
    remover = FakePrefixUninstall(h.events)
    writable = FakeEnsureWritable(h.events)

    result = await _prefix_installer(h, remover, writable).uninstall("tai42/toolbox")

    assert result["uninstalled"] is True
    # Removed from the prefix directly — pip is never shelled on the prefix path.
    assert remover.calls == [("tai42-toolbox", _PREFIX)]
    assert h.pip.calls == []
    # The deregister reload still lands BEFORE the prefix removal + row delete.
    order = [e for e in h.events if e in ("cm:write", "reload", "prefix:uninstall", "store:delete")]
    assert order == ["cm:write", "reload", "prefix:uninstall", "store:delete"]


async def test_install_unwind_with_prefix_removes_the_package_from_the_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer_module.importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    spec = make_spec()
    h.registry.resolved = make_resolved(spec, version="1.0.0")
    # The attribution write (step 5) fails AFTER the manifest apply persisted, so
    # the unwind restores the manifest and then removes the freshly-installed
    # package — from the prefix, never via pip uninstall.
    h.store.record_error = RuntimeError("attribution write failed")
    remover = FakePrefixUninstall(h.events)
    writable = FakeEnsureWritable(h.events)

    with pytest.raises(RuntimeError, match="attribution write failed"):
        await _prefix_installer(h, remover, writable).install("tai42/toolbox")

    assert remover.calls == [("tai42-toolbox", _PREFIX)]
    assert all(call[0] != "uninstall" for call in h.pip.calls)


async def test_update_with_prefix_removes_old_then_installs_new_into_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer_module.importlib.metadata, "version", lambda name: "0.1.0")
    old = make_spec(version="1.0.0", provides=_tool_provides("pkg.old"))
    new = make_spec(version="2.0.0", provides=_tool_provides("pkg.new"))
    h = Harness(manifest={"tools": [{"title": "pkg.old", "module": "pkg.old"}]})
    h.store.preload(old, version="1.0.0")
    h.registry.resolved = make_resolved(new, version="2.0.0")
    remover = FakePrefixUninstall(h.events)
    writable = FakeEnsureWritable(h.events)

    result = await _prefix_installer(h, remover, writable).update("tai42/toolbox")

    assert result["version"] == "2.0.0"
    # The old version leaves the prefix BEFORE the new install targets it — pip
    # cannot upgrade a distribution it does not see on the prefix path.
    assert remover.calls == [("tai42-toolbox", _PREFIX)]
    assert h.events.index("prefix:uninstall") < h.events.index("pip")
    assert h.pip.calls[0][3:5] == ["--prefix", _PREFIX]
    assert h.pip.calls[0][-1] == "tai42-toolbox==2.0.0"


# -- prefix: the environment-shadow install rule (four quadrants) -------------


async def test_install_prefix_env_same_version_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Quadrant 1 — env-same: the environment already provides the distribution at
    # the SAME version being installed. The prefix install is a harmless no-op
    # there (nothing lands), but the manifest wiring is the value, so the install
    # PROCEEDS and is recorded — never refused.
    monkeypatch.setattr(installer_module.importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.registry.resolved = make_resolved(spec, version="1.0.0")
    remover = FakePrefixUninstall(h.events)
    writable = FakeEnsureWritable(h.events)

    result = await _prefix_installer(h, remover, writable, env_version="1.0.0").install("tai42/toolbox")

    assert result["version"] == "1.0.0"
    # The manifest was patched and the attribution row written.
    assert h.svc.writes[-1]["tools"] == [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]
    assert h.store.record_calls[-1][0] == "tai42/toolbox"
    # pip still targeted the prefix (a no-op there, resolved against the environment).
    assert h.pip.calls[0][3:5] == ["--prefix", _PREFIX]


async def test_install_prefix_env_different_version_refused_before_state_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Quadrant 2 — env-different: the environment provides the distribution at a
    # DIFFERENT version. The prefix sits at the end of sys.path, so a prefix install
    # would be shadowed and never import — refused loudly BEFORE any state change,
    # naming both versions. No pip, no manifest write, no attribution.
    monkeypatch.setattr(installer_module.importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    spec = make_spec()
    h.registry.resolved = make_resolved(spec, version="2.0.0")
    remover = FakePrefixUninstall(h.events)
    writable = FakeEnsureWritable(h.events)

    with pytest.raises(EnvironmentShadowError) as exc:
        await _prefix_installer(h, remover, writable, env_version="1.5.0").install("tai42/toolbox")
    assert "1.5.0" in str(exc.value)
    assert "2.0.0" in str(exc.value)
    _assert_no_pip(h)
    assert h.svc.writes == []
    assert h.store.record_calls == []


async def test_uninstall_prefix_env_present_tolerant_drops_row_removes_no_files() -> None:
    # Quadrant 1 uninstall — the env-shadowed no-op install landed nothing in the
    # prefix, so uninstall strips the manifest, reloads, drops the row, and removes
    # NO files. The tolerant outcome rides the response notes, and the row is gone
    # (the old defect left it dangling forever).
    h = Harness(manifest={"tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.store.preload(spec, version="1.0.0")
    remover = FakePrefixUninstall(h.events)
    writable = FakeEnsureWritable(h.events)

    result = await _prefix_installer(h, remover, writable, env_version="1.0.0", prefix_has=False).uninstall(
        "tai42/toolbox"
    )

    assert result["uninstalled"] is True
    assert remover.calls == []  # no files removed from the prefix
    # Manifest stripped + reloaded, then the row dropped — no prefix:uninstall between.
    order = [e for e in h.events if e in ("cm:write", "reload", "prefix:uninstall", "store:delete")]
    assert order == ["cm:write", "reload", "store:delete"]
    assert "tai42/toolbox" not in h.store.rows
    assert any("absent from the plugin prefix" in n and "1.0.0" in n for n in result["notes"])


async def test_uninstall_prefix_absent_everywhere_raises_and_keeps_row() -> None:
    # Quadrant 4 uninstall — absent from BOTH the prefix and the environment is a
    # loud failure. The manifest is stripped (converged) but the row is KEPT so a
    # fixed re-run can finish: strip -> remove -> drop-row ordering halts on the
    # removal fault with the row intact (the tolerant path, not a reorder, closes
    # the env-shadowed trap; a genuine removal fault must still stop loudly).
    h = Harness(manifest={"tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.store.preload(spec, version="1.0.0")
    remover = FakePrefixUninstall(h.events)
    writable = FakeEnsureWritable(h.events)

    with pytest.raises(PluginPrefixError, match="neither the plugin prefix"):
        await _prefix_installer(h, remover, writable, env_version=None, prefix_has=False).uninstall("tai42/toolbox")
    assert remover.calls == []
    assert "store:delete" not in h.events
    assert "tai42/toolbox" in h.store.rows


async def test_uninstall_prefix_present_removes_files_no_note() -> None:
    # Quadrant 3 uninstall — present in the prefix: files are removed from the
    # prefix and no tolerant note is added.
    h = Harness(manifest={"tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}]})
    spec = make_spec(provides=[{"kind": "tool", "name": "gen-uuid", "module": "pkg.tools.uuid", "description": "d"}])
    h.store.preload(spec, version="1.0.0")
    remover = FakePrefixUninstall(h.events)
    writable = FakeEnsureWritable(h.events)

    result = await _prefix_installer(h, remover, writable, prefix_has=True).uninstall("tai42/toolbox")

    assert remover.calls == [("tai42-toolbox", _PREFIX)]
    assert not any("absent from the plugin prefix" in n for n in result["notes"])
    assert "tai42/toolbox" not in h.store.rows


async def test_install_unwind_prefix_env_present_removes_no_files(monkeypatch: pytest.MonkeyPatch) -> None:
    # The env-same install proceeds but the attribution write then fails. The unwind
    # restores the manifest and removes the freshly-installed package — but nothing
    # landed in the prefix (env-shadowed no-op), so the removal is a tolerant skip,
    # never a spurious prefix error masking the real attribution failure.
    monkeypatch.setattr(installer_module.importlib.metadata, "version", lambda name: "0.1.0")
    h = Harness()
    spec = make_spec()
    h.registry.resolved = make_resolved(spec, version="1.0.0")
    h.store.record_error = RuntimeError("attribution write failed")
    remover = FakePrefixUninstall(h.events)
    writable = FakeEnsureWritable(h.events)

    with pytest.raises(RuntimeError, match="attribution write failed"):
        await _prefix_installer(h, remover, writable, env_version="1.0.0", prefix_has=False).install("tai42/toolbox")
    assert remover.calls == []  # tolerant skip, no spurious PluginPrefixError
    assert h.svc.writes[-1] == {}  # the manifest was restored


async def test_update_prefix_env_shadow_refused_before_removal(monkeypatch: pytest.MonkeyPatch) -> None:
    # An update cannot install a version the environment shadows: with the env
    # providing the distribution, the target (necessarily a different version) is
    # refused BEFORE the old prefix wheel is removed and before any pip call.
    monkeypatch.setattr(installer_module.importlib.metadata, "version", lambda name: "0.1.0")
    old = make_spec(version="1.0.0", provides=_tool_provides("pkg.old"))
    new = make_spec(version="2.0.0", provides=_tool_provides("pkg.new"))
    h = Harness(manifest={"tools": [{"title": "pkg.old", "module": "pkg.old"}]})
    h.store.preload(old, version="1.0.0")
    h.registry.resolved = make_resolved(new, version="2.0.0")
    remover = FakePrefixUninstall(h.events)
    writable = FakeEnsureWritable(h.events)

    with pytest.raises(EnvironmentShadowError) as exc:
        await _prefix_installer(h, remover, writable, env_version="1.0.0").update("tai42/toolbox")
    assert "1.0.0" in str(exc.value)
    assert "2.0.0" in str(exc.value)
    assert remover.calls == []
    _assert_no_pip(h)
    assert h.svc.writes == []
