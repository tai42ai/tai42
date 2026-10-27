"""Discovery of a chain-declaring plugin installed in editable mode.

An editable install records none of its package files in its ``.dist-info`` RECORD — only
the ``.pth`` file whose path line puts the source tree on ``sys.path``. Build backends differ
in what else they write: setuptools adds a ``top_level.txt``; hatchling
(``_editable_impl_<name>.pth``) and uv_build (``<name>.pth``) write no ``top_level.txt``, and
flit and pdm-backend write the same ``<name>.pth`` shape uv_build does. The layouts below are
the files those backends leave in the site directory. In each, the packaged ``tai-plugin.yml``
is found import-free where the interpreter finds the package, so ``tai db migrate`` applies
its chain and the boot-time schema gate checks it.
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
import tai42_kit.db.migrations as kit_migrations
from tai42_kit.db import ComponentStatus, MigrationEntry, SchemaOutOfDateError, discover_migrations
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.db import boot_gate
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace.compat import distribution_map

_DIST = "tai42-editprobe"
_TOP_LEVEL = "tai42_editprobe"

_SPEC_YAML = f"""\
spec_version: 1
namespace: tai42
name: editprobe
package: {_DIST}
version: 1.0.0
description: An editable-installed test plugin
license: Apache-2.0
contract: ">=0.1,<1000"
categories: [dev]
provides:
  - kind: tool
    name: e
    module: {_TOP_LEVEL}.tools
    description: d
migrations: migrations
"""


@dataclass(frozen=True)
class _Layout:
    """What one build backend's editable install leaves in the site directory."""

    pth_name: str
    generator: str
    top_level_txt: bool


_LAYOUTS = {
    "setuptools": _Layout(f"__editable__.{_TOP_LEVEL}-1.0.0.pth", "setuptools (80.9.0)", top_level_txt=True),
    "hatchling": _Layout(f"_editable_impl_{_TOP_LEVEL}.pth", "hatchling 1.32.4", top_level_txt=False),
    "uv_build": _Layout(f"{_TOP_LEVEL}.pth", "uv 0.11.26", top_level_txt=False),
}


@pytest.fixture(params=sorted(_LAYOUTS))
def editable_probe(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Lay an editable install out: a source tree, and a site dir holding only its metadata and ``.pth``.

    The ``.pth`` path line is what the interpreter adds to ``sys.path`` when it processes the
    site directory at startup; the fixture puts the source tree on ``sys.path`` the same way.
    The package ``__init__`` raises, so any import of it fails the test.
    """
    layout = _LAYOUTS[request.param]
    source = tmp_path / "project" / "src"
    package = source / _TOP_LEVEL
    (package / "migrations").mkdir(parents=True)
    (package / "__init__.py").write_text("raise RuntimeError('discovery must never import the package')\n")
    (package / "tools.py").write_text("")
    (package / "tai-plugin.yml").write_text(_SPEC_YAML)
    (package / "migrations" / "0001_baseline.sql").write_text("CREATE TABLE editprobe_items (id integer);\n")

    site = tmp_path / "site"
    site.mkdir()
    (site / layout.pth_name).write_text(f"{source}\n")
    dist_info = site / f"{_TOP_LEVEL}-1.0.0.dist-info"
    dist_info.mkdir()
    metadata = {
        "METADATA": f"Metadata-Version: 2.4\nName: {_DIST}\nVersion: 1.0.0\n",
        "WHEEL": f"Wheel-Version: 1.0\nGenerator: {layout.generator}\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "INSTALLER": "uv\n",
        "direct_url.json": json.dumps({"url": (tmp_path / "project").as_uri(), "dir_info": {"editable": True}}),
    }
    if layout.top_level_txt:
        metadata["top_level.txt"] = f"{_TOP_LEVEL}\n"
    for name, body in metadata.items():
        (dist_info / name).write_text(body)
    rows = [f"{layout.pth_name},,", *(f"{dist_info.name}/{name},," for name in metadata), f"{dist_info.name}/RECORD,,"]
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n")

    monkeypatch.setattr(sys, "path", [str(site), str(source), *sys.path])
    monkeypatch.setattr("tai42_skeleton.marketplace.prefix.configured_prefix", lambda: None)
    importlib.invalidate_caches()
    yield package
    sys.modules.pop(_TOP_LEVEL, None)
    importlib.invalidate_caches()


def test_editable_install_chain_is_discovered_by_migrate(monkeypatch: pytest.MonkeyPatch, editable_probe: Path) -> None:
    from tai42_skeleton.db import installed_plugin_entries

    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    reset_all_settings()

    entries = {entry.component: entry for entry in installed_plugin_entries()}

    assert _DIST in entries, sorted(entries)
    assert Path(str(entries[_DIST].migrations_dir)) == editable_probe / "migrations"
    assert _TOP_LEVEL not in sys.modules


async def test_editable_install_chain_is_checked_by_the_boot_gate(
    monkeypatch: pytest.MonkeyPatch, editable_probe: Path
) -> None:
    checked: list[MigrationEntry] = []

    async def status(entries: list[MigrationEntry]) -> list[ComponentStatus]:
        checked.extend(entries)
        return [
            ComponentStatus(
                component=entry.component,
                applied_versions=(),
                pending=tuple(discover_migrations(entry.migrations_dir)),
                mismatches=(),
            )
            for entry in entries
        ]

    monkeypatch.setattr(kit_migrations, "migration_status", status)
    monkeypatch.setattr(boot_gate, "component_store_configured", lambda component: component == _DIST)
    monkeypatch.setattr(boot_gate, "component_store_settings", lambda component: SimpleNamespace(pg_dsn="runtime"))
    manifest = Manifest.model_validate(
        {"default_routers": "none", "tools": [{"title": "E", "module": f"{_TOP_LEVEL}.tools"}]}
    )

    with pytest.raises(SchemaOutOfDateError, match=f"{_DIST}: 1 pending \\(0001_baseline.sql\\)"):
        await boot_gate.assert_deployment_schemas_applied(manifest, distribution_map())

    assert [Path(str(entry.migrations_dir)) for entry in checked] == [editable_probe / "migrations"]
    assert _TOP_LEVEL not in sys.modules


def test_editable_install_without_a_packaged_spec_contributes_nothing(
    monkeypatch: pytest.MonkeyPatch, editable_probe: Path
) -> None:
    from tai42_skeleton.db import installed_plugin_entries

    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    reset_all_settings()
    (editable_probe / "tai-plugin.yml").unlink()

    assert _DIST not in {entry.component for entry in installed_plugin_entries()}
