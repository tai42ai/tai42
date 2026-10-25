"""Every delete of a stored object goes through the resource manager, where a held object is refused.

A storage provider's ``delete`` / ``delete_dir`` is called only from
``template/resource_manager.py``; a call anywhere else would remove an object a stored
object still names. A provider is recognised by its receiver: any ``delete_dir`` call, and
a ``delete`` call whose receiver names a provider or a storage.
"""

from __future__ import annotations

import ast
from pathlib import Path

import tai42_skeleton

_SKELETON_SRC = Path(tai42_skeleton.__file__).parent
_SEAM = "template/resource_manager.py"


def _receiver_names(node: ast.expr) -> list[str]:
    return [
        sub.id if isinstance(sub, ast.Name) else sub.attr
        for sub in ast.walk(node)
        if isinstance(sub, ast.Name | ast.Attribute)
    ]


def _is_provider_delete(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "delete_dir":
        return True
    if func.attr != "delete":
        return False
    return any("provider" in name.lower() or "storage" in name.lower() for name in _receiver_names(func.value))


def _provider_delete_sites() -> list[str]:
    sites: list[str] = []
    for path in sorted(_SKELETON_SRC.rglob("*.py")):
        rel = path.relative_to(_SKELETON_SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sites += [
            f"{rel}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_provider_delete(node)
        ]
    return sites


def test_only_the_resource_manager_deletes_through_a_storage_provider() -> None:
    sites = _provider_delete_sites()
    assert [site for site in sites if not site.startswith(f"{_SEAM}:")] == []
    assert len(sites) == 2


def test_the_check_recognises_a_provider_delete() -> None:
    def calls(source: str) -> list[bool]:
        return [_is_provider_delete(node) for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]

    assert calls("provider.delete(p)") == [True]
    assert calls("_storage_provider().delete(p)") == [True, False]
    assert calls("x.delete_dir(p)") == [True]
    assert calls("self._store.delete(p)") == [False]
