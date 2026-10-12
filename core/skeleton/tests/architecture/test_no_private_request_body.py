"""No skeleton code reads or writes Starlette's private ``Request._body`` slot.

A door that has already read the bounded body hands the bytes on through public seams
(``request_with_body``, an explicit ``raw`` argument), so no later read depends on an undocumented
framework attribute. A class's own ``_body`` member, reached through ``self`` or ``cls`` inside
the class that defines it, is that class's attribute and not the request's slot.
"""

from __future__ import annotations

import ast
from pathlib import Path

import tai42_skeleton

_SKELETON_SRC = Path(tai42_skeleton.__file__).parent


def _defines_body(cls: ast.ClassDef) -> bool:
    """Whether ``cls`` defines its own ``_body`` member: a method, or an assignment in its body."""
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_body":
            return True
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if any(isinstance(target, ast.Name) and target.id == "_body" for target in targets):
            return True
    return False


def _private_body_reads(tree: ast.AST) -> list[int]:
    """The lines that reach a ``_body`` slot on anything but the enclosing class's own ``_body`` member."""
    lines: list[int] = []

    def visit(node: ast.AST, own_body: bool) -> None:
        if isinstance(node, ast.ClassDef):
            own_body = _defines_body(node)
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "_body"
            and not (own_body and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"))
        ):
            lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, own_body)

    visit(tree, own_body=False)
    return lines


def test_no_private_request_body_access() -> None:
    offenders = []
    for path in sorted(_SKELETON_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(f"{path.relative_to(_SKELETON_SRC)}:{line}" for line in _private_body_reads(tree))
    assert offenders == []


def test_a_request_body_slot_is_flagged_and_a_class_own_body_member_is_not() -> None:
    source = (
        "async def door(request):\n"
        "    return request._body\n"
        "class Bridge:\n"
        "    async def _body(self):\n"
        "        return None\n"
        "    def run(self):\n"
        "        return self._body()\n"
        "class Other:\n"
        "    def read(self):\n"
        "        return self._body\n"
    )
    assert _private_body_reads(ast.parse(source)) == [2, 10]
