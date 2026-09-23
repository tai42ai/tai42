"""The internal cross-mixin seam of the composed store.

The methods each concern mixin reaches through the assembled :class:`PostgresStatesStore`'s MRO, declared
once so every mixin type-checks its sibling calls. The concrete implementations live on the connection,
record-read and record-write mixins.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import OrderedDict
from contextlib import AbstractAsyncContextManager
from typing import Any

from psycopg import AsyncConnection
from tai42_contract.states.models import CompletedOrigin, StateSubject


class _StoreBase(ABC):
    """The cross-mixin method contract every concern mixin builds on."""

    # The version-gated attachments-composition cache, keyed ``(state, declaration.updated_at)``
    # off the ``FOR SHARE``-locked declaration row and holding ``(regime_paths, traced_paths)``.
    # The composed store owns the one instance; the write mixin reads and populates it.
    _attachment_paths_cache: OrderedDict[tuple[str, Any], tuple[Any, Any]]

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
