"""Keyset-paged subject/record listing, containment search, and the write-ledger query."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tai42_contract.states.models import StateSubject

from .base import _StoreBase
from .connection import _pool, _settings
from .cursors import _split_cursor


class _RecordQueryStore(_StoreBase):
    """Keyset-paged subject/record listing + the write-ledger query."""

    async def list_subjects(
        self, state: str, *, kind: str | None, limit: int, cursor: str | None, conn: AsyncConnection[Any] | None = None
    ) -> list[dict[str, Any]]:
        """One keyset page of a state's subjects, ordered by the FULL subject identity
        ``(target_kind, target_name, subject_kind, subject_key)`` (optionally one
        ``kind``), each row ``{target_kind, target_name, kind, key, updated_at}``, starting
        strictly after ``cursor`` (the packed identity of the last row seen). With ``conn``
        the read joins the caller's transaction."""
        after = ("", "", "", "") if cursor is None else _split_cursor(cursor)
        async with self._read_cursor(conn) as cur:
            if kind is None:
                await cur.execute(
                    "SELECT target_kind, target_name, subject_kind, subject_key, "
                    "extract(epoch FROM updated_at)::float8 AS updated_at FROM state_records "
                    "WHERE state = %s AND (target_kind, target_name, subject_kind, subject_key) > (%s, %s, %s, %s) "
                    "ORDER BY target_kind, target_name, subject_kind, subject_key LIMIT %s",
                    (state, *after, limit),
                )
            else:
                await cur.execute(
                    "SELECT target_kind, target_name, subject_kind, subject_key, "
                    "extract(epoch FROM updated_at)::float8 AS updated_at FROM state_records "
                    "WHERE state = %s AND (target_kind, target_name, subject_kind, subject_key) > (%s, %s, %s, %s) "
                    "AND subject_kind = %s ORDER BY target_kind, target_name, subject_kind, subject_key LIMIT %s",
                    (state, *after, kind, limit),
                )
            return list(await cur.fetchall())

    async def search_records(
        self, state: str, containment: dict[str, Any], *, limit: int, cursor: str | None
    ) -> list[dict[str, Any]]:
        """One keyset page of the subjects whose record data CONTAINS ``containment``
        (``data @> containment``, the GIN ``jsonb_path_ops`` index serving it), ordered
        and cursored over the FULL subject identity ``(target_kind, target_name,
        subject_kind, subject_key)``."""
        after = ("", "", "", "") if cursor is None else _split_cursor(cursor)
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT target_kind, target_name, subject_kind, subject_key, "
                "extract(epoch FROM updated_at)::float8 AS updated_at FROM state_records "
                "WHERE state = %s AND (target_kind, target_name, subject_kind, subject_key) > (%s, %s, %s, %s) "
                "AND data @> %s::jsonb ORDER BY target_kind, target_name, subject_kind, subject_key LIMIT %s",
                (state, *after, Jsonb(containment), limit),
            )
            return list(await cur.fetchall())

    async def writes(
        self, state: str, subject: StateSubject, *, limit: int, cursor: str | None
    ) -> list[dict[str, Any]]:
        """One keyset page of a subject's write ledger, newest first (by ``id`` DESC),
        starting strictly before ``cursor`` (the last ``id`` seen). The subject resolves
        through the alias table so the audit trail follows a fold."""
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            kind, key = await self._resolve_subject(cur, state, subject)
            base = (
                "SELECT id, seq, at, door, actor, consumer, meta, run_id, turn_id, paths, op_id FROM state_writes "
                "WHERE state = %s AND target_kind = %s AND target_name = %s AND subject_kind = %s AND subject_key = %s"
            )
            if cursor is None:
                await cur.execute(
                    base + " ORDER BY id DESC LIMIT %s",
                    (state, subject.target_kind, subject.target_name, kind, key, limit),
                )
            else:
                await cur.execute(
                    base + " AND id < %s ORDER BY id DESC LIMIT %s",
                    (state, subject.target_kind, subject.target_name, kind, key, int(cursor), limit),
                )
            return list(await cur.fetchall())
