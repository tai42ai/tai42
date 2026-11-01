"""The manifest-write pipeline of a host-side process: a process with no serving surface.

``HostSideAdmin`` and ``host_side_combo_check`` are its collaborators; ``ConfigService.host_side()``
wires them, after the boot read, with the deployment's config manager and the bus the read built.
Its write stage persists a manifest change under the config store's exclusive write span; nothing
in the process reloads it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.data import load_manifest

from tai42_skeleton.app.instance import app
from tai42_skeleton.config.file_manager import FileConfigManager
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.config.service.host_side import HostSideAdmin, host_side_combo_check


@pytest.fixture(autouse=True)
def _restore_process_env() -> Iterator[None]:
    """The boot read writes the real process env; restore it and the settings caches."""
    from tai42_skeleton.app import applied_env

    snapshot = dict(os.environ)
    loaded_before = set(applied_env._loaded_env_keys)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    applied_env._loaded_env_keys = loaded_before
    reset_all_settings()


def test_the_host_side_admin_refuses_a_reload() -> None:
    with pytest.raises(RuntimeError, match="serves nothing to reload"):
        HostSideAdmin().reload_config()


async def test_the_host_side_admin_refuses_to_list_served_mcp_servers() -> None:
    with pytest.raises(RuntimeError, match="cannot list the MCP servers"):
        await HostSideAdmin().served_mcp_names(cast("Any", {}))


def test_the_host_side_combo_check_leaves_every_combo_to_the_next_start_bind() -> None:
    assert host_side_combo_check("t", [{"extension": "no.registry.knows.this"}]) is None


class _SpanRecordingManager(FileConfigManager):
    """A file config manager recording each exclusive write span and each persist inside one."""

    def __init__(self, config_dir: str) -> None:
        super().__init__(config_dir_path=config_dir)
        self.events: list[str] = []
        self._in_span = False

    @contextmanager
    def exclusive_write(self) -> Iterator[None]:
        with super().exclusive_write():
            self.events.append("span:open")
            self._in_span = True
            try:
                yield
            finally:
                self._in_span = False
                self.events.append("span:close")

    def mutate_manifest(self, mutator: Any) -> dict[str, Any]:
        self.events.append("persist:in-span" if self._in_span else "persist:outside-span")
        return super().mutate_manifest(mutator)


async def test_a_host_side_write_after_the_boot_read_persists_under_the_write_span_and_reloads_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)
    reset_all_settings()
    manifest = {
        "default_routers": "none",
        "tools": [{"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"}, {"title": "keep", "module": "keep.mod"}],
    }
    (tmp_path / "manifest.yml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    manager = _SpanRecordingManager(str(tmp_path))
    monkeypatch.setattr(app, "_config_manager", manager)
    monkeypatch.setattr(app, "_bus", None)
    await app.lifecycle.read_boot_manifest()

    def strip(document: dict[str, Any]) -> None:
        document["tools"] = [row for row in document["tools"] if row["title"] != "pkg.tools.uuid"]

    persisted = await ConfigService.host_side().persist_change(strip)

    assert manager.events == ["span:open", "persist:in-span", "span:close"]
    assert persisted.document["tools"] == [{"title": "keep", "module": "keep.mod"}]
    assert load_manifest((tmp_path / "manifest.yml").read_text())["tools"] == [{"title": "keep", "module": "keep.mod"}]


async def test_a_host_side_write_refuses_an_invalid_manifest_with_nothing_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)
    reset_all_settings()
    text = yaml.safe_dump(
        {"default_routers": "none", "tools": [{"title": "keep", "module": "keep.mod"}]}, sort_keys=False
    )
    (tmp_path / "manifest.yml").write_text(text)
    monkeypatch.setattr(app, "_config_manager", FileConfigManager(config_dir_path=str(tmp_path)))
    monkeypatch.setattr(app, "_bus", None)
    await app.lifecycle.read_boot_manifest()

    def misspell(document: dict[str, Any]) -> None:
        document["tools"][0]["modul"] = "x"

    with pytest.raises(ValueError, match="modul"):
        await ConfigService.host_side().persist_change(misspell)

    assert (tmp_path / "manifest.yml").read_text() == text
