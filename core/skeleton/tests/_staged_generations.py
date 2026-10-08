"""Snapshot and restore every per-generation global an epoch build stages and promotes.

``build_and_swap_epoch`` drives ``registry_staging``'s ``begin/commit/abort_staging_all``.
A test that runs it with an injected no-op ``rebuild`` seam registers nothing into the
staged generation, so a successful build commits EMPTY generations over the process-wide
registries (the route registry's shape index included). The router and plugin modules were
imported once per worker, so nothing repopulates them and every later test on that worker
sees empty registries.

The participant set is read from the staging functions themselves: every module-level
global of ``registry_staging`` that ``begin/commit/abort_staging_all`` reference. A global
newly staged there is therefore snapshotted here with no edit to this file.

Each participant keeps its generations inside the kit's staged-registry primitives (every
class defined in ``tai42_kit.registry.staged``), reached through the participant's own
attributes and through primitives that wrap other primitives. The snapshot covers the
participant's attribute state and the attribute state of every primitive reached, so a
promotion that swaps a primitive's committed container is undone on restore. A new
primitive class defined there is covered with no edit to this file.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from types import FunctionType
from typing import Any

from tai42_kit.registry import staged as staged_primitives

from tai42_skeleton.app import registry_staging

_STAGING_PHASES: tuple[FunctionType, ...] = (
    registry_staging.begin_staging_all,
    registry_staging.commit_staging_all,
    registry_staging.abort_staging_all,
)

_STAGED_PRIMITIVE_TYPES: tuple[type, ...] = tuple(
    value
    for value in vars(staged_primitives).values()
    if isinstance(value, type) and value.__module__ == staged_primitives.__name__
)


def staged_primitives_of(participant: Any) -> list[Any]:
    """Every kit staged primitive reachable from ``participant``'s attributes, nested ones included.

    The walk follows the participant's own attributes and then each primitive's attributes, so a
    registry wrapping a staged generation (a named-factory registry) yields both objects.
    """
    found: list[Any] = []
    seen: set[int] = set()
    pending = [participant]
    while pending:
        holder = pending.pop()
        for value in vars(holder).values():
            if isinstance(value, _STAGED_PRIMITIVE_TYPES) and id(value) not in seen:
                seen.add(id(value))
                found.append(value)
                pending.append(value)
    return found


def staged_participants() -> dict[str, Any]:
    """Every ``registry_staging`` global the three staging phases reference, by name.

    Raises ``TypeError`` for a referenced global that holds no attribute state to snapshot
    (a function, or a container of participants), or that reaches no kit staged primitive
    (its generations live somewhere this helper cannot find), so a restructured staging
    module or registry fails the suite loudly instead of leaving a participant unrestored.
    """
    namespace = vars(registry_staging)
    participants: dict[str, Any] = {}
    for phase in _STAGING_PHASES:
        for name in phase.__code__.co_names:
            if name not in namespace:
                continue  # an attribute name (``commit_staging``), not a module global
            value = namespace[name]
            if isinstance(value, FunctionType) or not hasattr(value, "__dict__"):
                raise TypeError(
                    f"registry_staging.{name} ({type(value).__name__}) is referenced by a staging phase "
                    "but holds no attribute state to snapshot; extend this helper to cover it"
                )
            if not isinstance(value, _STAGED_PRIMITIVE_TYPES) and not staged_primitives_of(value):
                raise TypeError(
                    f"registry_staging.{name} ({type(value).__name__}) is referenced by a staging phase "
                    "but reaches no staged-registry primitive whose generations could be saved; "
                    "extend this helper to cover it"
                )
            participants[name] = value
    return participants


def saved_objects() -> list[Any]:
    """Every object whose attribute state the restore covers: each participant and its primitives."""
    objects: list[Any] = []
    seen: set[int] = set()
    for participant in staged_participants().values():
        for obj in (participant, *staged_primitives_of(participant)):
            if id(obj) not in seen:
                seen.add(id(obj))
                objects.append(obj)
    return objects


def _snapshot(obj: Any) -> dict[str, Any]:
    """The non-dunder attribute state of ``obj``, with mutable containers shallow-copied."""
    return {
        name: copy.copy(value) if isinstance(value, dict | list | set) else value
        for name, value in vars(obj).items()
        if not name.startswith("__")
    }


def _restore(obj: Any, saved: dict[str, Any]) -> None:
    """Put ``obj``'s non-dunder attribute state back to ``saved`` exactly."""
    for name in [n for n in vars(obj) if not n.startswith("__") and n not in saved]:
        delattr(obj, name)
    for name, value in saved.items():
        setattr(obj, name, value)


@contextmanager
def preserved_staged_generations() -> Iterator[None]:
    """Restore every staged participant's committed and staged state when the block exits."""
    saved = [(obj, _snapshot(obj)) for obj in saved_objects()]
    try:
        yield
    finally:
        for obj, state in saved:
            _restore(obj, state)
