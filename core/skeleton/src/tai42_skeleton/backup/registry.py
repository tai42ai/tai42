"""Concrete ``AppBackup`` registry behind the ``app.backup`` facet.

A section is a named ``(exporter, importer)`` pair plus a ``secret`` flag, stored
in registration order. An exporter may be sync or async; :meth:`export_section`
returns its result verbatim (a coroutine for an async section) without awaiting —
pure name-to-callable dispatch, and the caller awaits. An importer is awaited and
its result VALIDATED against :class:`BackupSectionReport`: a wrong shape raises
:class:`BackupSectionReportError` naming the section rather than reaching the
platform as an opaque object it digs keys out of.

The per-import ``skip``/``overwrite`` mode rides a request-scoped contextvar, not
the ``import_section`` signature (which is the vendor-neutral contract shape):
:func:`import_mode` binds it for a whole import, each mode-aware importer reads it
via :func:`current_import_mode`. Default ``skip`` (non-destructive) outside any import.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError
from tai42_contract.backup import BackupSectionInfo, BackupSectionReport

# Per keyed record: ``skip`` leaves an existing record untouched, ``overwrite``
# upserts it; new records are created under both.
BackupMode = Literal["skip", "overwrite"]

_import_mode: ContextVar[BackupMode] = ContextVar("tai42_backup_import_mode", default="skip")


class BackupSectionReportError(ValueError):
    """An importer returned a value that is not a valid ``BackupSectionReport``.

    Raised by :meth:`BackupRegistry.import_section` naming the section, so the
    restore route reports it under that section's errors exactly as it does a
    raising importer — never a silently mis-shaped report reaching the platform.
    """


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
        """Create an empty registry."""
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
        """Register a section under ``name``.

        A duplicate name raises rather than silently overwrite an existing section.
        """
        if name in self._sections:
            raise ValueError(f"backup section {name!r} is already registered")
        self._sections[name] = _Section(name=name, exporter=exporter, importer=importer, secret=secret)

    def sections(self) -> list[BackupSectionInfo]:
        """Every registered section as a ``BackupSectionInfo``, in registration order."""
        return [BackupSectionInfo(name=section.name, secret=section.secret) for section in self._sections.values()]

    def export_section(self, name: str) -> Any:
        """Run ``name``'s exporter and return its payload. Unknown name raises."""
        return self._require(name).exporter()

    async def import_section(self, name: str, payload: Any) -> BackupSectionReport:
        """Run ``name``'s importer over ``payload`` and return its validated report.

        Awaits an async importer, then validates the result against
        :class:`BackupSectionReport`. An unknown name raises; a result that is not a
        valid report raises :class:`BackupSectionReportError` naming the section.
        """
        result = self._require(name).importer(payload)
        if inspect.isawaitable(result):
            result = await result
        try:
            return BackupSectionReport.model_validate(result)
        except ValidationError as exc:
            raise BackupSectionReportError(
                f"backup section {name!r} importer returned an invalid report shape: {exc}"
            ) from exc

    def _require(self, name: str) -> _Section:
        try:
            return self._sections[name]
        except KeyError:
            raise KeyError(f"unknown backup section: {name!r}") from None
