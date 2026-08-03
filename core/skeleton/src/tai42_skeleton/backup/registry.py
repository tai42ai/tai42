"""Concrete ``AppBackup`` registry behind the ``app.backup`` facet.

A section is a named ``(exporter, importer)`` pair plus a ``secret`` flag, stored
in registration order. Exporter/importer may be sync or async; the registry
returns their result verbatim (a coroutine for an async section) without awaiting
or inspecting it — pure name-to-callable dispatch, and the caller awaits.

The per-import ``skip``/``overwrite`` mode rides a request-scoped contextvar, not
the ``import_section`` signature (which is the vendor-neutral contract shape):
:func:`import_mode` binds it for a whole import, each mode-aware importer reads it
via :func:`current_import_mode`. Default ``skip`` (non-destructive) outside any import.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from tai42_contract.backup import BackupSectionInfo

# Per keyed record: ``skip`` leaves an existing record untouched, ``overwrite``
# upserts it; new records are created under both.
BackupMode = Literal["skip", "overwrite"]

_import_mode: ContextVar[BackupMode] = ContextVar("tai42_backup_import_mode", default="skip")


def current_import_mode() -> BackupMode:
    """The mode of the import in progress, or ``skip`` outside an import."""
    return _import_mode.get()


@contextmanager
def import_mode(mode: BackupMode) -> Iterator[None]:
    """Bind ``mode`` for the duration of one import, restoring the prior value after."""
    token = _import_mode.set(mode)
    try:
        yield
    finally:
        _import_mode.reset(token)


@dataclass(frozen=True)
class _Section:
    """One registered section: its name, exporter/importer pair, and secrecy."""

    name: str
    exporter: Callable[[], Any]
    importer: Callable[[Any], Any]
    secret: bool


class BackupRegistry:
    """Ordered registry of named backup sections (``tai42_contract.app.AppBackup``)."""

    def __init__(self) -> None:
        # Insertion-ordered: ``sections()`` reports registration order.
        self._sections: dict[str, _Section] = {}

    def register_section(
        self,
        name: str,
        exporter: Callable[[], Any],
        importer: Callable[[Any], Any],
        *,
        secret: bool = False,
    ) -> None:
        """Register a section under ``name``. A duplicate name raises rather than
        silently overwrite an existing section."""
        if name in self._sections:
            raise ValueError(f"backup section {name!r} is already registered")
        self._sections[name] = _Section(name=name, exporter=exporter, importer=importer, secret=secret)

    def sections(self) -> list[BackupSectionInfo]:
        """Every registered section as a ``BackupSectionInfo``, in registration order."""
        return [BackupSectionInfo(name=section.name, secret=section.secret) for section in self._sections.values()]

    def export_section(self, name: str) -> Any:
        """Run ``name``'s exporter and return its payload. Unknown name raises."""
        return self._require(name).exporter()

    def import_section(self, name: str, payload: Any) -> Any:
        """Run ``name``'s importer over ``payload`` and return its report. Unknown
        name raises."""
        return self._require(name).importer(payload)

    def _require(self, name: str) -> _Section:
        try:
            return self._sections[name]
        except KeyError:
            raise KeyError(f"unknown backup section: {name!r}") from None
