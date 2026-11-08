"""Every registered operation that reaches a config door declares ``holds_writer_exit``.

A config door (:func:`~tai42_skeleton.app.writer_exit.require_writer_exit_held`) raises when a
change reaches it with no hold on the worker's exit, so an operation that writes the worker's own
configuration or soft-restarts its fleet must have its edge take the hold before it runs. This pin
reads it statically, before a request does: from each operation's own code, following every call it
makes into the platform's functions and methods (resolved through the calling module's names), an
operation whose calls reach a config write entrypoint or the fleet soft restart is a writer, and
every writer declares the hold.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable
from typing import Any

from tai42_skeleton.app.route_registry import load_api_routes
from tai42_skeleton.operations.registry import operation_registry

# The config service's write entrypoints and its fleet soft restart: every call reaching one is a
# change to this worker's own configuration or fleet.
_CONFIG_DOORS = frozenset(
    {
        "apply_change",
        "persist_change",
        "converge",
        "reapply_manifest",
        "apply_replace",
        "apply_env_change",
        "apply_derived_env_change",
        "apply_env_and_change",
        "apply_replace_env",
        "reload_fleet",
    }
)

_PACKAGE = "tai42_skeleton."

# Declared writers whose door the static read cannot follow: the backup import reaches each
# section's importer through the app's backup facet, bound at run time.
_DECLARED_THROUGH_A_FACET = frozenset({"import_backup"})


def _platform_callable(obj: Any) -> Callable[..., Any] | None:
    """``obj`` as a platform function to follow, or ``None`` when it is not one."""
    obj = getattr(obj, "__func__", obj)
    if not (inspect.isfunction(obj) or inspect.iscoroutinefunction(obj)):
        return None
    func = inspect.unwrap(obj)
    # Code generated at run time (a dataclass's ``__init__``) has no source and writes nothing.
    generated = func.__code__.co_filename.startswith("<")
    return func if func.__module__.startswith(_PACKAGE) and not generated else None


def _owner_class(func: Callable[..., Any]) -> type | None:
    """The class a method is defined on, read from its qualified name; ``None`` for a module function."""
    owner: Any = inspect.getmodule(func)
    path = func.__qualname__.split(".")[:-1]
    if not path or "<locals>" in path:
        return None
    for name in path:
        owner = inspect.getattr_static(owner, name)
    return owner if isinstance(owner, type) else None


class _Resolver:
    """Resolves the callee expressions of one function to the objects they name in its module."""

    def __init__(self, func: Callable[..., Any]) -> None:
        self._globals = func.__globals__
        self._owner = _owner_class(func)

    def resolve(self, node: ast.expr) -> Any:
        if isinstance(node, ast.Name):
            if node.id in ("self", "cls") and self._owner is not None:
                return self._owner
            return self._globals.get(node.id)
        if isinstance(node, ast.Attribute):
            # Read statically: resolving a name never runs a property or a module's lazy attribute.
            base = self.resolve(node.value)
            return None if base is None else inspect.getattr_static(base, node.attr, None)
        if isinstance(node, ast.Call):
            # A constructed instance resolves to its class, whose methods its attributes name.
            called = self.resolve(node.func)
            return called if isinstance(called, type) else None
        return None


def _reaches_a_config_door(func: Callable[..., Any], seen: set[Callable[..., Any]]) -> bool:
    if func in seen:
        return False
    seen.add(func)
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    resolver = _Resolver(func)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in _CONFIG_DOORS:
            return True
        callee = resolver.resolve(node.func)
        if isinstance(callee, type):
            callee = inspect.getattr_static(callee, "__init__", None)
        followed = _platform_callable(callee)
        if followed is not None and _reaches_a_config_door(followed, seen):
            return True
    return False


def _writers() -> set[str]:
    return {op.name for op in operation_registry.all() if _reaches_a_config_door(inspect.unwrap(op.func), set())}


def test_every_operation_reaching_a_config_door_holds_the_writer_exit() -> None:
    list(load_api_routes())
    writers = _writers()
    declared = {op.name for op in operation_registry.all() if op.holds_writer_exit}

    assert sorted(writers - declared) == []
    # The read sees every declared writer but the ones reached through a facet: it is not vacuous.
    assert sorted(declared - writers) == sorted(_DECLARED_THROUGH_A_FACET)
