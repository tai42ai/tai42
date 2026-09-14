"""Alias-aware ``state_records`` reads — subject resolution, single-record and view reads,
and the backup exporter's record/alias reads."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from tai42_contract.states.models import StateSubject

from .base import _StoreBase
from .connection import _pool, _settings


class _RecordReadStore(_StoreBase):
    """Alias-aware reads over ``state_records`` and ``state_subject_aliases``."""

    async def _resolve_subject(self, cur: Any, state: str, subject: StateSubject) -> tuple[str, str]:
        """The canonical ``(kind, key)`` for ``subject`` — one indexed lookup on the
        CALLER's cursor, so it joins the caller's transaction (and its declaration-row
        lock, which serializes against every fold). No alias row ⇒ the subject IS its own
        canonical. Target scope never changes across a fold."""
        await cur.execute(
            "SELECT canonical_kind, canonical_key FROM state_subject_aliases "
            "WHERE state = %s AND target_kind = %s AND target_name = %s AND alias_kind = %s AND alias_key = %s",
            (state, subject.target_kind, subject.target_name, subject.kind, subject.key),
        )
        row = await cur.fetchone()
        return (subject.kind, subject.key) if row is None else (row["canonical_kind"], row["canonical_key"])

    async def read_record(self, state: str, subject: StateSubject) -> tuple[dict[str, Any] | None, float | None]:
        """``(data, seq)`` for ``subject`` — the subject resolves through the alias table
        (a folded key lands on the surviving record); ``(None, None)`` when absent."""
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            kind, key = await self._resolve_subject(cur, state, subject)
            await cur.execute(
                "SELECT data, extract(epoch FROM updated_at)::float8 AS seq FROM state_records "
                "WHERE state = %s AND target_kind = %s AND target_name = %s AND subject_kind = %s AND subject_key = %s",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            row = await cur.fetchone()
            return (None, None) if row is None else (row["data"], row["seq"])

    async def read_record_view(
        self, state: str, subject: StateSubject, *, conn: AsyncConnection[Any] | None = None
    ) -> dict[str, Any] | None:
        """The read door's view: ``{data, seq, canonical_subject, folded_from}`` —
        ``folded_from`` is every alias pointing at the canonical — or ``None`` when no
        record exists. With ``conn`` the read joins the caller's transaction (a attach
        reconciler reading its own in-flight merges)."""
        async with self._read_cursor(conn) as cur:
            kind, key = await self._resolve_subject(cur, state, subject)
            await cur.execute(
                "SELECT data, extract(epoch FROM updated_at)::float8 AS seq FROM state_records "
                "WHERE state = %s AND target_kind = %s AND target_name = %s AND subject_kind = %s AND subject_key = %s",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await cur.execute(
                "SELECT alias_kind, alias_key FROM state_subject_aliases "
                "WHERE state = %s AND target_kind = %s AND target_name = %s "
                "AND canonical_kind = %s AND canonical_key = %s ORDER BY alias_kind, alias_key",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            folded = [
                StateSubject(
                    target_kind=subject.target_kind,
                    target_name=subject.target_name,
                    kind=r["alias_kind"],
                    key=r["alias_key"],
                )
                for r in await cur.fetchall()
            ]
            canonical = StateSubject(
                target_kind=subject.target_kind, target_name=subject.target_name, kind=kind, key=key
            )
            return {
                "data": row["data"],
                "seq": row["seq"],
                "canonical_subject": canonical,
                "folded_from": folded,
            }

    async def export_records(self, state: str) -> list[dict[str, Any]]:
        """Every record of a state as export rows ``{target_kind, target_name,
        subject_kind, subject_key, data}`` — the backup exporter's read."""
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT target_kind, target_name, subject_kind, subject_key, data FROM state_records "
                "WHERE state = %s ORDER BY target_kind, target_name, subject_kind, subject_key",
                (state,),
            )
            return list(await cur.fetchall())

    async def list_aliases(self, state: str) -> list[dict[str, Any]]:
        """Every subject-alias row of a state — the backup exporter's read."""
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT target_kind, target_name, alias_kind, alias_key, canonical_kind, canonical_key, mode "
                "FROM state_subject_aliases WHERE state = %s "
                "ORDER BY target_kind, target_name, alias_kind, alias_key",
                (state,),
            )
            return list(await cur.fetchall())
