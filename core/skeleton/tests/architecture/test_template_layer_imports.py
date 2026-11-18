"""The template layer imports, of the skeleton, only itself.

The template layer is the storage feature's implementation; the storage, tools, operations and
app packages compose over it. A module under ``tai42_skeleton/template/`` imports, of the
skeleton, only ``tai42_skeleton.template.*``; it raises its own typed errors
(``template/errors.py``) and the operations door (``operations/template_door.py``) translates
them to a status. Every import counts, a function-local one included.
"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import tai42_skeleton

_SKELETON_SRC = Path(tai42_skeleton.__file__).parent
_LAYER = _SKELETON_SRC / "template"


def _package_of(rel: str) -> str:
    """The dotted package a module at ``rel`` (relative to the skeleton source) belongs to."""
    parts = ["tai42_skeleton", *rel.removesuffix(".py").split("/")]
    return ".".join(parts[:-1])


def _skeleton_modules(node: ast.Import | ast.ImportFrom, package: str) -> list[str]:
    """The absolute module names ``node`` imports; a relative import is resolved against ``package``."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level == 0:
        return [node.module] if node.module is not None else []
    base = resolve_name("." * node.level + (node.module or ""), package)
    if node.module is None:
        return [f"{base}.{alias.name}" for alias in node.names]
    return [base]


def _upward_imports() -> list[str]:
    sites: list[str] = []
    for path in sorted(_LAYER.rglob("*.py")):
        rel = path.relative_to(_SKELETON_SRC).as_posix()
        package = _package_of(rel)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module in _skeleton_modules(node, package):
                in_skeleton = module == "tai42_skeleton" or module.startswith("tai42_skeleton.")
                in_layer = module == "tai42_skeleton.template" or module.startswith("tai42_skeleton.template.")
                if in_skeleton and not in_layer:
                    sites.append(f"{rel}:{node.lineno} {module}")
    return sites


def test_the_template_layer_imports_only_itself_of_the_skeleton() -> None:
    assert _upward_imports() == []


def test_the_guard_sees_the_layer() -> None:
    # The walk reads real modules: an empty layer would pass the guard vacuously.
    assert (_LAYER / "store_writes.py").is_file()


def test_a_relative_import_is_resolved_against_its_package() -> None:
    package = _package_of("template/sub/mod.py")
    relative_up = ast.parse("from ...app import instance").body[0]
    relative_bare = ast.parse("from ... import operations").body[0]
    relative_in = ast.parse("from .. import errors").body[0]
    assert isinstance(relative_up, ast.ImportFrom)
    assert isinstance(relative_bare, ast.ImportFrom)
    assert isinstance(relative_in, ast.ImportFrom)
    assert _skeleton_modules(relative_up, package) == ["tai42_skeleton.app"]
    assert _skeleton_modules(relative_bare, package) == ["tai42_skeleton.operations"]
    assert _skeleton_modules(relative_in, package) == ["tai42_skeleton.template.errors"]
