"""The child side of the cross-process ``ConfigService`` write tests.

Each child is a worker over the one shared ``FileConfigManager`` directory: it applies the
stored env the way the boot bridge does, binds a fresh app, and drives one ``ConfigService``
write. A ``spawn`` child imports the function it runs by this module's ``tests.config`` name.
"""

from __future__ import annotations

import asyncio
import os
from multiprocessing.connection import Connection
from typing import Any

from tai42_skeleton.config.file_manager import FileConfigManager

# The bound a child waits on the other child's or the parent's event.
EVENT_BOUND_S = 60.0


def shared_manifest() -> dict[str, Any]:
    """A manifest whose ``m`` server picks its tool by ``M_PICK`` and filters on ``M_SYSTEM``."""
    return {"mcp": [{"title": "m", "config": {"url": "http://x"}, "include": ["!ENV ${M_PICK}", "!ENV ${M_SYSTEM}"]}]}


class _EnvPersistSignallingManager(FileConfigManager):
    """A file config manager setting ``persisted`` once a whole-env replace has landed."""

    def __init__(self, config_dir: str, persisted: Any) -> None:
        super().__init__(config_dir_path=config_dir)
        self._persisted = persisted

    def replace_env(self, config: dict[str, str]) -> None:
        super().replace_env(config)
        self._persisted.set()


def _worker_service(config_dir: str, manager: FileConfigManager) -> Any:
    """Boot a busless worker over ``config_dir`` and build its ``ConfigService``.

    It captures the deployment env, applies the stored env and binds the app, as a boot does.
    """
    for key in ("TAI_BUS_REDIS_URL", "TAI_MANIFEST_PATH"):
        os.environ.pop(key, None)
    os.environ["TAI_CONFIG_DIR_PATH"] = config_dir
    os.environ["M_SYSTEM"] = "m_c"
    from tai42_contract.app import tai42_app
    from tests._fakes.bus import FakeBus
    from tests.config.fake_pipeline import ComboChecks, FakeReloadAdmin

    from tai42_skeleton.app import instance
    from tai42_skeleton.app.applied_env import apply_env_and_reset_settings
    from tai42_skeleton.config.deployment_env import DeploymentOwnership, capture_deployment_env
    from tai42_skeleton.config.service import ConfigService

    capture_deployment_env()
    apply_env_and_reset_settings(manager.read_env(), DeploymentOwnership())
    tai42_app.bind(instance.build_app())

    async def list_active_bodies(_facet: Any) -> dict[str, Any]:
        return {}

    type(instance.app.presets).list_active_bodies = list_active_bodies  # type: ignore[method-assign]
    return ConfigService(config_manager=manager, admin=FakeReloadAdmin(), bus=FakeBus(), combo_check=ComboChecks())


def _report(out: Connection, outcome: str) -> None:
    out.send(outcome)
    out.close()


def profile_dropping_a_key(
    config_dir: str, in_build: Any, other_writing: Any, persisted: Any, other_waiting: Any, out: Connection
) -> None:
    """Apply a profile that drops ``M_GONE``; its build runs on until ``other_waiting`` is set.

    The parent sets ``other_waiting`` once it sees the other worker blocked on the store's write
    span, which this apply holds through its build and its persist.
    """
    from tai42_skeleton.app.epoch import Epoch
    from tai42_skeleton.config.deployment_env import DeploymentOwnership

    service = _worker_service(config_dir, _EnvPersistSignallingManager(config_dir, persisted))

    async def build(_env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
        in_build.set()
        if not await asyncio.to_thread(other_writing.wait, EVENT_BOUND_S):
            raise TimeoutError("the other worker never started its write")
        if not await asyncio.to_thread(other_waiting.wait, EVENT_BOUND_S):
            raise TimeoutError("the other worker's write was never seen waiting on the write span")
        return Epoch(number=0)

    async def save_previous(_env: dict[str, str]) -> None:
        return None

    async def release_pools() -> None:
        return None

    async def main() -> str:
        await service.apply_replace_env(
            {"M_PICK": "m_b"},
            save_previous=save_previous,
            build_and_swap=build,
            build_and_discard=build,
            release_llm_pools=release_pools,
        )
        return "persisted"

    _report(out, asyncio.run(main()))


def marker_on_the_dropped_key(
    config_dir: str, in_build: Any, other_writing: Any, persisted: Any, out: Connection
) -> None:
    """Once the other worker's profile build runs, store a marker on ``M_GONE``; report how it ended.

    The report says whether the profile's env had persisted by the time this write was refused.
    """
    service = _worker_service(config_dir, FileConfigManager(config_dir_path=config_dir))

    def add_marker(document: dict[str, Any]) -> None:
        document["mcp"][0]["config"]["headers"] = {"X-Race": "!ENV ${M_GONE}"}

    async def main() -> str:
        if not await asyncio.to_thread(in_build.wait, EVENT_BOUND_S):
            raise TimeoutError("the other worker's build never started")
        other_writing.set()
        try:
            await service.apply_change(add_marker)
        except ValueError as exc:
            return f"refused (profile persisted first: {persisted.is_set()}): {exc}"
        return "persisted"

    _report(out, asyncio.run(main()))
