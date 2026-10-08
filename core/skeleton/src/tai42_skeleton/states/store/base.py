"""The internal cross-mixin seam of the composed store.

The methods each concern mixin reaches through the assembled :class:`PostgresStatesStore`'s MRO, declared
once so every mixin type-checks its sibling calls. The concrete implementations live on the connection,
record-read and record-write mixins.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from psycopg import AsyncConnection
from tai42_contract.states.models import CompletedOrigin, StateSubject


class ApplyEntry(Protocol):
    """What a write path applies a declaration version with: the document validator and the composed paths."""

    @property
    def validator(self) -> Draft202012Validator:
        """Validates a whole record document against the effective schema."""
        ...

    @property
    def regime_paths(self) -> list[tuple[list[Any], str, str]]:
        """The absolute regime rules ``(path, regime, template)`` over every attachment."""
        ...

    @property
    def traced_paths(self) -> tuple[tuple[str | int, ...], ...]:
        """The attach paths under which a write stamps ``_trace``."""
        ...


class ApplyEntrySource(Protocol):
    """Serves the :class:`ApplyEntry` for a declaration ``version`` read under the write's lock, on its cursor."""

    async def write_entry(self, cur: Any, state: str, version: int) -> ApplyEntry:
        """The entry built at ``version``; a miss loads it on ``cur`` (the write's transaction)."""
        ...


class _StoreBase(ABC):
    """The cross-mixin method contract every concern mixin builds on."""

    @abstractmethod
    def _write_cursor(self, conn: AsyncConnection[Any] | None) -> AbstractAsyncContextManager[Any]:
        """The cursor a write runs on — a new pooled transaction, or the caller's when ``conn`` is threaded in."""

    @abstractmethod
    def _read_cursor(self, conn: AsyncConnection[Any] | None) -> AbstractAsyncContextManager[Any]:
        """The cursor a read runs on — a plain pooled connection, or the caller's transaction when threaded in."""

    @abstractmethod
    async def _resolve_subject(self, cur: Any, state: str, subject: StateSubject) -> tuple[str, str]:
        """The canonical ``(kind, key)`` for ``subject``, resolved through the alias table on the cursor."""

    @staticmethod
    @abstractmethod
    async def _insert_write(
        cur: Any,
        state: str,
        tk: str,
        tn: str,
        kind: str,
        key: str,
        seq: float | None,
        origin: CompletedOrigin,
        paths: list[list[Any]],
        op_id: str | None,
    ) -> None:
        """Record one ``state_writes`` provenance row in the caller's transaction."""
