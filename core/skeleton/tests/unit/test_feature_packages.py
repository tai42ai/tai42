"""tai42_skeleton ships its own ``tools/`` and ``hooks/`` feature packages; each
imports and exposes its entry symbol."""

import importlib


def test_tools_package_present() -> None:
    assert hasattr(importlib.import_module("tai42_skeleton.tools"), "ToolRegistry")


def test_hooks_package_present() -> None:
    assert hasattr(importlib.import_module("tai42_skeleton.hooks"), "get_hooks_manager")
