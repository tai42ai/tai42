"""An env write whose awaits run while something it was validated against changes.

``apply_env_change`` and ``apply_replace_env`` validate, then await the held-unpick check (its
stored-preset read and server listing), then write. A change landing in that window — the
process env, the stored manifest, or the live serving generation — is validated again before
anything is written. ``apply_replace_env`` validates once more right before its build, so a
change landing while it snapshots ``@previous`` is refused too.
"""

from __future__ import annotations

import os
from typing import Any, cast

import pytest
from tai42_contract.presets import PresetBody

from tai42_skeleton.app import epoch as epoch_module
from tai42_skeleton.app import instance
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.config.deployment_env import DeploymentOwnership
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.tools.binding.errors import ExtensionComboRefusedError

from .._fakes.bus import FakeBus
from .fake_pipeline import (
    ComboChecks,
    FakeConfigStore,
    FakeReloadAdmin,
    _no_bus,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
)

_LABELLED = {"name": "labelled", "config": {"label": "y"}}


def _manifest() -> dict[str, Any]:
    # ``M_PICK`` picks the ``m`` server's tool, so repointing it changes the selection and runs
    # the held-unpick check; ``M_SYSTEM`` lives only in the process env, never in the store.
    return {
        "tools": [
            {
                "title": "a",
                "module": "tests.app._fixtures.tools_check",
                "include": ["echo"],
                "extensions": {"echo": [[_LABELLED]]},
            }
        ],
        "mcp": [{"title": "m", "config": {"url": "http://x"}, "include": ["!ENV ${M_PICK}", "!ENV ${M_SYSTEM}"]}],
    }


class _World:
    """A store, a service over it, and a hook run while the held-unpick check awaits."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, combo_check: Any = None) -> None:
        _no_bus(monkeypatch)
        monkeypatch.setenv("M_PICK", "m_a")
        monkeypatch.setenv("M_SYSTEM", "m_c")
        monkeypatch.setenv("M_GONE", "1")
        self.store = FakeConfigStore(manifest=_manifest(), env={"M_PICK": "m_a", "M_GONE": "1"})
        self.service = ConfigService(
            config_manager=self.store,
            admin=FakeReloadAdmin(),
            bus=cast("Any", FakeBus()),
            combo_check=combo_check or ComboChecks(),
        )
        self.during: Any = None
        self.on_save_previous: Any = None
        self.builds: list[dict[str, str]] = []
        world = self

        async def list_active_bodies(_facet: Any) -> dict[str, PresetBody]:
            if world.during is not None:
                world.during()
            return {}

        monkeypatch.setattr(type(instance.app.presets), "list_active_bodies", list_active_bodies)

    async def write(self, door: str, env: dict[str, str]) -> None:
        if door == "env_change":
            await self.service.apply_env_change(env)
            return

        async def save_previous(_env: dict[str, str]) -> None:
            if self.on_save_previous is not None:
                self.on_save_previous()

        async def build(build_env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
            self.builds.append(dict(build_env))
            return Epoch(number=0)

        async def discard(build_env: dict[str, str]) -> None:
            self.builds.append(dict(build_env))

        async def release() -> None:
            return None

        await self.service.apply_replace_env(
            {"M_PICK": env["M_PICK"]},
            save_previous=save_previous,
            build_and_swap=build,
            build_and_discard=discard,
            release_llm_pools=release,
        )


_DOORS = ["env_change", "replace_env"]


def _assert_nothing_written(world: _World) -> None:
    assert world.store.env_writes == []
    assert world.builds == []


@pytest.mark.parametrize("door", _DOORS)
async def test_a_process_env_key_dropped_during_the_check_is_refused_as_dangling(
    monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    world = _World(monkeypatch)
    world.during = lambda: os.environ.pop("M_SYSTEM", None)

    with pytest.raises(ValueError, match=r"markers dangling .*M_SYSTEM"):
        await world.write(door, {"M_PICK": "m_b", "M_GONE": ""})

    _assert_nothing_written(world)


@pytest.mark.parametrize("door", _DOORS)
async def test_a_manifest_marker_written_during_the_check_on_a_key_the_write_drops_is_refused(
    monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    # The env write drops ``M_GONE`` (emptied, or omitted by the profile); another writer
    # stores a marker on it while the check awaits.
    world = _World(monkeypatch)

    def reference_gone() -> None:
        world.store.manifest["mcp"][0]["config"]["headers"] = {"X-Race": "!ENV ${M_GONE}"}

    world.during = reference_gone

    with pytest.raises(ValueError, match=r"markers dangling .*M_GONE"):
        await world.write(door, {"M_PICK": "m_b", "M_GONE": ""})

    _assert_nothing_written(world)


async def test_a_manifest_marker_written_before_the_build_on_a_key_the_profile_drops_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The profile omits ``M_GONE``; the stored manifest gains a marker on it while the apply
    # snapshots ``@previous``, after its held-unpick check.
    world = _World(monkeypatch)

    def reference_gone() -> None:
        world.store.manifest["mcp"][0]["config"]["headers"] = {"X-Race": "!ENV ${M_GONE}"}

    world.on_save_previous = reference_gone

    with pytest.raises(ValueError, match=r"markers dangling .*M_GONE"):
        await world.write("replace_env", {"M_PICK": "m_b"})

    _assert_nothing_written(world)


@pytest.mark.parametrize("door", _DOORS)
async def test_a_serving_generation_swapped_during_the_check_rechecks_the_combos(
    monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    swapped = object()
    checks: list[bool] = []

    def combo_check(tool: str, combo: Any) -> None:
        checks.append(epoch_module.current_epoch_or_none() is swapped)
        if checks[-1]:
            raise ExtensionComboRefusedError(f"extension 'labelled' on tool '{tool}' cannot bind: refused")

    world = _World(monkeypatch, combo_check=combo_check)
    world.during = lambda: monkeypatch.setattr(epoch_module, "_current", swapped)

    with pytest.raises(ValueError, match=r"^tools\[a\]\.extensions\[echo\]\[0\]: .* cannot bind: refused$"):
        await world.write(door, {"M_PICK": "m_b", "M_GONE": ""})

    assert checks == [False, True]
    _assert_nothing_written(world)


@pytest.mark.parametrize("door", _DOORS)
async def test_an_env_write_nothing_changed_under_checks_each_combo_once(
    monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    checks = ComboChecks()
    world = _World(monkeypatch, combo_check=checks)

    await world.write(door, {"M_PICK": "m_b", "M_GONE": ""})

    assert checks.calls == [("echo", [_LABELLED])]
