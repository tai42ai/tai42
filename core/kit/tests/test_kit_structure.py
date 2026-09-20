"""Structural gate: the leaf rule (kit imports only tai42_contract)."""

from __future__ import annotations

import ast
from pathlib import Path

import tai42_kit

_SRC = Path(tai42_kit.__path__[0])


def test_leaf_rule_imports_only_tai_contract():
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module
                else []
            )
            for name in names:
                top = name.split(".")[0]
                if top.startswith("tai42_") and top not in ("tai42_kit", "tai42_contract"):
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, offenders
