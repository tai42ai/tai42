"""No skeleton code reads or writes Starlette's private ``Request._body`` slot.

A door that has already read the bounded body hands the bytes on through public seams
(``request_with_body``, an explicit ``raw`` argument), so no later read depends on an undocumented
framework attribute.
"""

from __future__ import annotations

import ast
from pathlib import Path

import tai42_skeleton

_SKELETON_SRC = Path(tai42_skeleton.__file__).parent


def test_no_private_request_body_access() -> None:
    offenders = []
    for path in sorted(_SKELETON_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(
            f"{path.relative_to(_SKELETON_SRC)}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "_body"
        )
    assert offenders == []
