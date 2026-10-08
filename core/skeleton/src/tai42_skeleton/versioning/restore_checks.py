"""The per-kind save rules the versioned-document restore runs before it writes a document.

The versioned-document store is body-opaque and kind-agnostic, so a kind that has save rules
registers them here, keyed by its ``kind``; the restore runs the kind's check over the ACTIVE
version body of each document it is about to write. A kind with no registered check restores
unchecked.

A registered check runs only the rules whose verdict depends on the document itself (its body,
and the stores the sections restored before it fill): the restore's import loop registers
nothing in the live process registries, so a rule judging the body against them would restore
the same document differently on a fresh and on a live deployment.

A check refuses a document by raising a ``ValueError`` or a binding refusal of the states store
(the restore skips that document and reports it); any other error is a store or transport
failure that fails the section.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: A restore check: called with the document row's ``name`` and its active version body.
RestoreCheck = Callable[[str, dict[str, Any]], Awaitable[None]]

_checks: dict[str, RestoreCheck] = {}


def register_restore_check(kind: str, check: RestoreCheck) -> None:
    """Register ``check`` as the restore check of ``kind``; a second registration for one kind raises."""
    if kind in _checks:
        raise ValueError(f"a restore check for versioned-document kind {kind!r} is already registered")
    _checks[kind] = check


def restore_check(kind: str) -> RestoreCheck | None:
    """The restore check registered for ``kind``, or ``None`` when the kind registers none."""
    return _checks.get(kind)


__all__ = ["RestoreCheck", "register_restore_check", "restore_check"]
