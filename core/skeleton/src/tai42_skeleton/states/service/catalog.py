"""The version-keyed catalog snapshot: per-process derived artifacts of declarations and templates.

Each entry carries the row ``version`` it was built from. A reader serves an entry only for the
version it read inside the statement its path already runs (a write's locked declaration read, a
catalog read's ``SELECT version`` probe), so an entry is never served across a change made by any
process: every writer draws a new version from one database sequence in the statement that changes
the row, so a version never repeats for a name, even across a delete and a re-create. A probe that
finds no row drops the entry. Only a successfully built entry is ever stored.
"""

from __future__ import annotations

import copy
import dataclasses
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from tai42_contract.states.models import StateRegimeRule

from tai42_skeleton.states.store.trace import _abs_regime_paths, _traced_paths

if TYPE_CHECKING:
    from tai42_skeleton.states.store import PostgresStatesStore
    from tai42_skeleton.states.templates import StateTemplate


@dataclass(frozen=True, slots=True)
class AttachmentRow:
    """One attachment on a state as the snapshot holds it, with the attached template's version and stored body."""

    template: str
    path: tuple[str, ...]
    parameters: dict[str, Any]
    declarations: dict[str, Any]
    template_version: int
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StateEntry:
    """A declaration's derived artifacts at one ``version``.

    ``validator`` validates a whole record document against ``effective_schema``;
    ``regime_paths``/``traced_paths`` are the composed absolute regime rules and traced attach
    prefixes the write path applies; ``served_regimes`` is the composed regime list a declaration
    read serves. ``row`` is the full declaration row, present once a catalog read loaded it (a
    write path builds the entry without it).
    """

    version: int
    effective_schema: dict[str, Any]
    validator: Draft202012Validator
    regime_paths: list[tuple[list[Any], str, str]]
    traced_paths: tuple[tuple[str | int, ...], ...]
    attachments: tuple[AttachmentRow, ...]
    served_regimes: tuple[StateRegimeRule, ...]
    row: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class TemplateEntry:
    """A template row at one ``version``: its stored ``body`` and, for an inline fragment, the validated template.

    A template whose fragment is stored by id carries ``template=None``: it is resolved through the
    resource manager on use, so it follows the stored resource.
    """

    version: int
    body: dict[str, Any]
    template: StateTemplate | None


def _attachment_rows(rows: list[dict[str, Any]]) -> tuple[AttachmentRow, ...]:
    return tuple(
        AttachmentRow(
            template=row["template"],
            path=tuple(row["path"] or []),
            parameters=dict(row["parameters"] or {}),
            declarations=dict(row["declarations"] or {}),
            template_version=int(row["template_version"]),
            body=row["body"] or {},
        )
        for row in rows
    )


def build_state_entry(
    version: int, effective_schema: dict[str, Any], attachment_rows: list[dict[str, Any]], row: dict[str, Any] | None
) -> StateEntry:
    """Build a :class:`StateEntry` from a declaration's effective schema and its attachments JOIN rows."""
    attachments = _attachment_rows(attachment_rows)
    served = tuple(
        StateRegimeRule(path=[*a.path, *rule["path"]], regime=rule["regime"])
        for a in attachments
        for rule in a.body.get("regimes") or []
    )
    return StateEntry(
        version=version,
        effective_schema=effective_schema,
        validator=Draft202012Validator(effective_schema),
        regime_paths=_abs_regime_paths(attachment_rows),
        traced_paths=_traced_paths(attachment_rows),
        attachments=attachments,
        served_regimes=served,
        row=row,
    )


class CatalogSnapshot:
    """The process-scope snapshot of declaration and template entries, one entry per catalog row.

    Mutations run under a ``threading.Lock`` that is never held across an ``await``; two concurrent
    misses both load and the higher version wins on insert.
    """

    def __init__(self, store: PostgresStatesStore) -> None:
        """Bind the store a miss loads through."""
        self._store = store
        self._lock = threading.Lock()
        self._states: dict[str, StateEntry] = {}
        self._templates: dict[str, TemplateEntry] = {}

    # -- states ------------------------------------------------------------------
    def state_at(self, name: str, version: int) -> StateEntry | None:
        """The entry for ``name`` built at exactly ``version``, or ``None``."""
        with self._lock:
            entry = self._states.get(name)
        return entry if entry is not None and entry.version == version else None

    def insert_state(self, name: str, entry: StateEntry) -> StateEntry:
        """Store ``entry`` unless a higher version is already held; return the entry now held for its version."""
        with self._lock:
            current = self._states.get(name)
            if current is not None and current.version > entry.version:
                return entry
            if (
                current is not None
                and current.version == entry.version
                and entry.row is None
                and current.row is not None
            ):
                return current
            self._states[name] = entry
            return entry

    def drop_state(self, name: str) -> None:
        """Forget ``name``'s entry (its row is gone)."""
        with self._lock:
            self._states.pop(name, None)

    async def write_entry(self, cur: Any, state: str, version: int) -> StateEntry:
        """The entry a write path validates and applies with, on the write's own transaction.

        ``version`` is the one the write read under its declaration lock. A miss reads the effective
        schema and the attachments on ``cur`` (the same transaction, so consistent with the lock)
        and inserts the entry.
        """
        cached = self.state_at(state, version)
        if cached is not None:
            return cached
        effective_schema, attachment_rows = await self._store.read_state_entry_rows(cur, state)
        return self.insert_state(state, build_state_entry(version, effective_schema, attachment_rows, None))

    async def catalog_entry(self, state: str) -> StateEntry | None:
        """The entry with the full declaration row, after one ``SELECT version`` probe; ``None`` when undeclared."""
        version = await self._store.declaration_version(state)
        if version is None:
            self.drop_state(state)
            return None
        return await self.catalog_entry_at(state, version)

    async def catalog_entry_at(self, state: str, version: int) -> StateEntry | None:
        """The entry with the full declaration row for a ``version`` the caller already read."""
        cached = self.state_at(state, version)
        if cached is not None and cached.row is not None:
            return cached
        row, attachment_rows = await self._store.read_state_catalog_rows(state)
        if row is None:
            self.drop_state(state)
            return None
        read_version = int(row["version"])
        existing = self.state_at(state, read_version)
        if existing is not None:
            # A write path built this version's artifacts already: keep them (the same validator
            # object serves every write at one version) and add the row.
            return self.insert_state(state, dataclasses.replace(existing, row=row))
        return self.insert_state(state, build_state_entry(read_version, row["effective_schema"], attachment_rows, row))

    # -- templates ---------------------------------------------------------------
    def template_at(self, name: str, version: int) -> TemplateEntry | None:
        """The template entry for ``name`` at exactly ``version``, or ``None``."""
        with self._lock:
            entry = self._templates.get(name)
        return entry if entry is not None and entry.version == version else None

    def insert_template(self, name: str, entry: TemplateEntry) -> TemplateEntry:
        """Store ``entry`` unless a higher version is already held; return ``entry``.

        A loader overtaken by a newer version still answers with the entry it built for the version
        it read, so every reader serves the version its own statement returned.
        """
        with self._lock:
            current = self._templates.get(name)
            if current is None or current.version <= entry.version:
                self._templates[name] = entry
        return entry

    def drop_template(self, name: str) -> None:
        """Forget ``name``'s template entry (its row is gone)."""
        with self._lock:
            self._templates.pop(name, None)

    def entry_counts(self) -> tuple[int, int]:
        """``(state entries, template entries)`` currently held."""
        with self._lock:
            return len(self._states), len(self._templates)


def served_row(entry: StateEntry) -> dict[str, Any]:
    """A deep copy of the entry's declaration row, so a caller's mutation never reaches the snapshot."""
    if entry.row is None:
        raise AssertionError("a catalog entry carries its declaration row")
    return copy.deepcopy(entry.row)
