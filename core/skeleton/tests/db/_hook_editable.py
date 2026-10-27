"""A neutral chain-declaring plugin installed in an import-hook editable mode, on disk.

An import-hook editable install records none of its packages in installed metadata: its
``.dist-info`` holds no ``top_level.txt``, its ``RECORD`` lists only a ``.pth`` file, the
finder module that file imports and that module's cached bytecode, and the ``.pth`` holds an
``import`` line instead of a path.
When ``site`` processes the site directory it runs that line, which puts a finder on
``sys.meta_path``; the finder serves the package from the project's source tree. The PEP 610
``direct_url.json`` names that project directory as an editable source.

Two finder shapes are laid out, each self-contained:

- ``editables``: the shape hatchling's ``dev-mode-exact`` and pdm-backend's ``editables``
  mode write — ``_editable_impl_<name>.pth`` importing a module whose finder, appended to
  ``sys.meta_path``, answers the top-level name with a file-location spec of the source
  ``__init__.py``.
- ``meson``: the shape meson-python writes — ``<name>-editable.pth`` importing a loader
  module and calling its ``init()``, which inserts a finder at ``sys.meta_path[0]``. Its
  package spec has a VIRTUAL search location (a path inside the loader module) and its loader
  serves the package's files only through its resource reader, as a virtual directory tree
  whose files are real paths, as meson-python's ``MesonpyReader``/``MesonpyTraversable`` do.

The package ``__init__`` raises, so any import of it fails the test.
"""

from __future__ import annotations

import importlib
import json
import site
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

DIST = "tai42-hookprobe"
TOP_LEVEL = "tai42_hookprobe"
TOOLS_MODULE = f"{TOP_LEVEL}.tools"
CHAIN_FILE = "0001_baseline.sql"

SHAPES = ("editables", "meson")

_EDITABLES_FINDER = """\
import importlib.abc
import importlib.util
import sys


class _Redirector(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if path is not None or fullname != {top_level!r}:
            return None
        return importlib.util.spec_from_file_location(fullname, {init_path!r})


sys.meta_path.append(_Redirector())
"""

_MESON_LOADER = """\
import importlib.machinery
import os
import pathlib
import sys
from importlib.resources.abc import Traversable, TraversableResources


def _tree(directory):
    tree = {{}}
    for entry in os.scandir(directory):
        tree[entry.name] = _tree(entry.path) if entry.is_dir() else entry.path
    return tree


class _Missing(Traversable):
    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    def is_dir(self):
        return False

    def is_file(self):
        return False

    def iterdir(self):
        raise FileNotFoundError(self._name)

    def open(self, *args, **kwargs):
        raise FileNotFoundError(self._name)

    def joinpath(self, *descendants):
        return _Missing(str(descendants[-1]).split("/")[-1]) if descendants else self


class _VirtualDir(Traversable):
    def __init__(self, name, tree):
        self._name = name
        self._tree = tree

    @property
    def name(self):
        return self._name

    def is_dir(self):
        return True

    def is_file(self):
        return False

    def iterdir(self):
        for name, node in self._tree.items():
            yield _VirtualDir(name, node) if isinstance(node, dict) else pathlib.Path(node)

    def open(self, *args, **kwargs):
        raise IsADirectoryError(self._name)

    def joinpath(self, *descendants):
        if not descendants:
            return self
        names = [part for descendant in descendants for part in str(descendant).split("/")]
        node = self._tree.get(names[0])
        if isinstance(node, dict):
            return _VirtualDir(names[0], node).joinpath(*names[1:])
        if isinstance(node, str):
            return pathlib.Path(node).joinpath(*names[1:])
        return _Missing(names[0]).joinpath(*names[1:])


class _Reader(TraversableResources):
    def __init__(self, name, tree):
        self._name = name
        self._tree = tree

    def files(self):
        return _VirtualDir(self._name, self._tree)


class _Loader(importlib.machinery.SourceFileLoader):
    def __init__(self, name, path, tree):
        super().__init__(name, path)
        self._tree = tree

    def get_resource_reader(self, name):
        return _Reader(name, self._tree)


class _Finder:
    def __init__(self):
        self._tree = _tree({package_dir!r})

    def find_spec(self, fullname, path=None, target=None):
        if fullname != {top_level!r}:
            return None
        loader = _Loader(fullname, {init_path!r}, self._tree)
        spec = importlib.machinery.ModuleSpec(fullname, loader, origin={init_path!r})
        spec.has_location = True
        spec.submodule_search_locations = [os.path.join(__file__, fullname)]
        return spec


def init():
    sys.meta_path.insert(0, _Finder())
"""


def finder_module_name(shape: str, dist: str = DIST) -> str:
    """The module the ``shape`` install's ``.pth`` imports: the finder the build backend generates."""
    stem = dist.replace("-", "_")
    if shape == "editables":
        return f"_editable_impl_{stem}"
    if shape == "meson":
        return f"_{stem}_editable_loader"
    raise ValueError(f"unknown hook shape {shape!r}")


def spec_yaml() -> str:
    """The packaged ``tai-plugin.yml``: a tool provider declaring a ``migrations`` chain."""
    return (
        "spec_version: 1\n"
        "namespace: tai42\n"
        "name: hookprobe\n"
        f"package: {DIST}\n"
        "version: 1.0.0\n"
        "description: A neutral hook-served editable test plugin\n"
        "license: Apache-2.0\n"
        "contract: '>=0.1,<1000'\n"
        "categories: [dev]\n"
        "provides:\n"
        "  - kind: tool\n"
        "    name: hookprobe_ping\n"
        f"    module: {TOOLS_MODULE}\n"
        "    description: d\n"
        "migrations: migrations\n"
    )


@dataclass(frozen=True)
class HookInstall:
    """Where one laid-out hook-served editable install lives."""

    site: Path
    project: Path
    package: Path
    finder_module: str


def lay_out(
    tmp_path: Path,
    shape: str,
    *,
    spec_text: str | None,
    requires_contract: str | None = None,
    dist: str = DIST,
    project: Path | None = None,
) -> HookInstall:
    """Write the project source tree and the site directory one ``shape`` install leaves.

    ``spec_text`` is the packaged ``tai-plugin.yml`` (``None``: the package ships none, a
    library). ``requires_contract`` is the ``tai42-contract`` range its metadata declares.
    ``project`` places the project directory (default ``tmp_path / "project"``).
    """
    project = project or tmp_path / "project"
    package = project / "src" / TOP_LEVEL
    (package / "migrations").mkdir(parents=True)
    init_path = package / "__init__.py"
    init_path.write_text("raise RuntimeError('discovery must never import the package')\n")
    (package / "tools.py").write_text("")
    if spec_text is not None:
        (package / "tai-plugin.yml").write_text(spec_text)
    (package / "migrations" / CHAIN_FILE).write_text("CREATE TABLE hookprobe_items (id integer);\n")

    site_dir = tmp_path / "site"
    site_dir.mkdir(exist_ok=True)
    finder_module = finder_module_name(shape, dist)
    if shape == "editables":
        pth_name = f"{finder_module}.pth"
        pth_body = f"import {finder_module}\n"
        finder_source = _EDITABLES_FINDER.format(top_level=TOP_LEVEL, init_path=str(init_path))
    else:
        pth_name = f"{dist}-editable.pth"
        pth_body = f"import {finder_module}; {finder_module}.init()"
        finder_source = _MESON_LOADER.format(top_level=TOP_LEVEL, init_path=str(init_path), package_dir=str(package))
    (site_dir / pth_name).write_text(pth_body)
    (site_dir / f"{finder_module}.py").write_text(finder_source)

    dist_info = site_dir / f"{dist.replace('-', '_')}-1.0.0.dist-info"
    dist_info.mkdir()
    metadata_text = f"Metadata-Version: 2.4\nName: {dist}\nVersion: 1.0.0\n"
    if requires_contract is not None:
        metadata_text += f"Requires-Dist: tai42-contract{requires_contract}\n"
    metadata = {
        "METADATA": metadata_text,
        "WHEEL": "Wheel-Version: 1.0\nGenerator: hookprobe\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "INSTALLER": "pip\n",
        "direct_url.json": json.dumps({"url": project.as_uri(), "dir_info": {"editable": True}}),
    }
    for name, body in metadata.items():
        (dist_info / name).write_text(body)
    # pip byte-compiles the finder module on install and records the cached bytecode.
    cached = f"__pycache__/{finder_module}.{sys.implementation.cache_tag}.pyc"
    (site_dir / "__pycache__").mkdir(exist_ok=True)
    (site_dir / cached).write_bytes(b"")
    rows = [
        f"{cached},,",
        f"{pth_name},,",
        f"{finder_module}.py,,",
        *(f"{dist_info.name}/{name},," for name in metadata),
        f"{dist_info.name}/RECORD,,",
    ]
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n")
    return HookInstall(site=site_dir, project=project, package=package, finder_module=finder_module)


def activate(monkeypatch: pytest.MonkeyPatch, install: HookInstall) -> None:
    """Process the site directory as ``site`` does at interpreter start, on restorable copies.

    ``site.addsitedir`` adds the directory to ``sys.path`` and runs the ``.pth`` ``import``
    line, which installs the finder; ``sys.path`` and ``sys.meta_path`` are swapped for copies
    first so the test leaves both as it found them.
    """
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    site.addsitedir(str(install.site))
    importlib.invalidate_caches()


def forget_modules(dist: str = DIST) -> None:
    """Drop the package and every shape's finder module from ``sys.modules``."""
    for name in (TOP_LEVEL, *(finder_module_name(shape, dist) for shape in SHAPES)):
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
