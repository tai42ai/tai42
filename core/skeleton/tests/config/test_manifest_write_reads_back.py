"""A manifest written through each ConfigService door reads back.

The doors drive the real :class:`~tai42_skeleton.config.file_manager.FileConfigManager`.
A blank line between a key and its block sequence, in the defaults or in the stored
manifest, must survive every dump the change, its validation and the store make: at the
top level, inside an entry a mutator re-wraps in a plain list, and behind a merge key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ruamel.yaml.comments import CommentedMap
from tai42_kit.utils.data.yaml_util import dump_manifest

from tai42_skeleton.config.file_manager import FileConfigManager

from .fake_pipeline import (  # noqa: F401  — autouse fixture, active on import
    _no_bus,
    _oauth_descriptor,
    _reset_settings_after,
    _service,
    _with_bus,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_BLANK_BEFORE_SEQUENCES = (
    "\nstudio_plugins:\n\n- plugin_a\n\n\n\nmcp:\n\n"
    "- title: base\n  config:\n    url: http://base\n  include:\n\n  - tool_a\n"
)
_MERGED_ENTRY = "- <<: *base\n  title: copy\n"


def _manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, defaults: str = "", stored: str | None = None
) -> FileConfigManager:
    _no_bus(monkeypatch)
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "manifest.yml").write_text(defaults, encoding="utf-8")
    if stored is not None:
        (tmp_path / "manifest.yml").write_text(stored, encoding="utf-8")
    return FileConfigManager(config_dir_path=str(tmp_path))


def _add(title: str) -> Any:
    def mutator(document: dict[str, Any]) -> None:
        document["mcp"] = [*(document.get("mcp") or []), {"title": title, "config": {"url": f"http://{title}"}}]

    return mutator


def _titles(manager: FileConfigManager) -> list[str]:
    return [entry["title"] for entry in manager.read_manifest()["mcp"]]


def _includes(manager: FileConfigManager) -> list[list[str]]:
    return [entry.get("include") for entry in manager.read_manifest()["mcp"]]


async def test_apply_change_over_defaults_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(tmp_path, monkeypatch, defaults=_BLANK_BEFORE_SEQUENCES)
    service, _admin, _bus = _service(manager)

    await service.apply_change(_add("first"))
    assert _titles(manager) == ["first"]
    assert manager.read_manifest()["studio_plugins"] == ["plugin_a"]

    await service.apply_change(_add("second"))
    assert _titles(manager) == ["first", "second"]
    assert manager.read_manifest()["studio_plugins"] == ["plugin_a"]


async def test_apply_change_over_a_stored_manifest_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(tmp_path, monkeypatch, stored=_BLANK_BEFORE_SEQUENCES)
    service, _admin, _bus = _service(manager)

    await service.apply_change(_add("added"))
    assert _titles(manager) == ["base", "added"]
    assert _includes(manager) == [["tool_a"], None]
    assert manager.read_manifest()["studio_plugins"] == ["plugin_a"]


async def test_apply_change_over_a_merge_key_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stored = _BLANK_BEFORE_SEQUENCES.replace("- title: base", "- &base\n  title: base") + _MERGED_ENTRY
    manager = _manager(tmp_path, monkeypatch, stored=stored)
    service, _admin, _bus = _service(manager)

    await service.apply_change(_add("added"))
    assert _titles(manager) == ["base", "copy", "added"]
    assert _includes(manager) == [["tool_a"], ["tool_a"], None]


async def test_apply_change_dropping_a_connector_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Dropping an oauth connector writes its secret name to the env and the manifest as one unit.
    stored = _BLANK_BEFORE_SEQUENCES + dump_manifest(CommentedMap(connectors=[_oauth_descriptor("acme")]))
    manager = _manager(tmp_path, monkeypatch, stored=stored)
    _with_bus(monkeypatch)
    service, _admin, _bus = _service(manager)

    def drop_and_add(document: dict[str, Any]) -> None:
        document["connectors"] = []
        _add("added")(document)

    await service.apply_change(drop_and_add)
    assert _titles(manager) == ["base", "added"]
    assert _includes(manager) == [["tool_a"], None]
    assert manager.read_manifest()["connectors"] == []
    assert "ACME_CLIENT_SECRET" in (tmp_path / ".env").read_text(encoding="utf-8")


async def test_apply_replace_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(tmp_path, monkeypatch, stored=_BLANK_BEFORE_SEQUENCES)
    service, _admin, _bus = _service(manager)

    document = service.read_manifest_document()
    document["mcp"] = [*document["mcp"], {"title": "replaced", "config": {"url": "http://replaced"}}]
    await service.apply_replace(document)

    assert _titles(manager) == ["base", "replaced"]
    assert _includes(manager) == [["tool_a"], None]
    assert manager.read_manifest()["studio_plugins"] == ["plugin_a"]
