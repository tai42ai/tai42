"""The record write path — replace, path-op apply, RTBF erase and subject fold.

Each records the ``state_writes`` provenance row and the ``state_applied_ops``
idempotency ledger.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tai42_contract.states.errors import StateNotFoundError, SubjectFoldError
from tai42_contract.states.models import CompletedOrigin, StateSubject

from tai42_skeleton.states.paths import apply_ops as apply_path_ops
from tai42_skeleton.states.paths import guard_passes

from .base import _StoreBase
from .connection import _pool, _settings
from .cursors import _subject_cols
from .trace import _abs_regime_paths, _iso_now, _refuse_composing_shape, _traced_paths, stamp_trace

# The attachments-composition cache ceiling, mirroring the service template cache's bound.
# Evicting the oldest past this keeps a busy process's memory bounded across many states.
_ATTACHMENT_PATHS_CACHE_MAX = 256


class _RecordWriteStore(_StoreBase):
    """The record write path plus the ``state_writes`` provenance row and ``state_applied_ops`` idempotency ledger."""

    @staticmethod
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
        """Record one ``state_writes`` row in the caller's transaction.

        The row carries the completed origin plus the touched paths.
        """
        await cur.execute(
            "INSERT INTO state_writes "
            "(state, target_kind, target_name, subject_kind, subject_key, seq, at, door, actor, consumer, meta, "
            "run_id, turn_id, paths, op_id) VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                state,
                tk,
                tn,
                kind,
                key,
                seq,
                origin.door,
                origin.actor,
                origin.consumer,
                Jsonb(origin.meta) if origin.meta is not None else None,
                origin.run_id,
                origin.turn_id,
                Jsonb(paths),
                op_id,
            ),
        )

    async def replace(
        self, state: str, subject: StateSubject, data: dict[str, Any], *, origin: CompletedOrigin, validate_doc: Any
    ) -> tuple[dict[str, Any], float]:
        """Replace ``subject``'s whole document with ``data`` and record the write.

        ``data`` is validated whole against the effective schema; the write records
        paths ``[[]]`` (the whole document). ONE txn under the declaration
        ``FOR SHARE`` lock; the subject resolves through the alias table first.
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT effective_schema FROM state_declarations WHERE name = %s FOR SHARE", (state,))
            decl = await cur.fetchone()
            if decl is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            kind, key = await self._resolve_subject(cur, state, subject)
            validate_doc(decl["effective_schema"], data)
            await cur.execute(
                "INSERT INTO state_records (state, target_kind, target_name, subject_kind, subject_key, data, "
                "updated_at) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp()) "
                "ON CONFLICT (state, target_kind, target_name, subject_kind, subject_key) DO UPDATE SET "
                "data = EXCLUDED.data, updated_at = clock_timestamp() "
                "RETURNING extract(epoch FROM updated_at)::float8 AS seq",
                (state, subject.target_kind, subject.target_name, kind, key, Jsonb(data)),
            )
            seq_row = await cur.fetchone()
            seq = None if seq_row is None else seq_row["seq"]
            await self._insert_write(
                cur, state, subject.target_kind, subject.target_name, kind, key, seq, origin, [[]], None
            )
            return data, float(seq or 0.0)

    async def _composed_attachment_paths(self, cur: Any, state: str, version: Any) -> tuple[Any, Any]:
        """The state's ``(regime_paths, traced_paths)``, version-gated by the declaration's ``updated_at``.

        Served from a bounded per-process cache keyed ``(state, version)``: a hit skips the
        attachments JOIN, a miss runs it and populates. Every attachment/declaration writer bumps
        ``updated_at`` under the ``FOR UPDATE`` lock, so a changed composition carries a new
        version and the key misses cross-process — a stale composition is never served.
        """
        cache_key = (state, version)
        cached = self._attachment_paths_cache.get(cache_key)
        if cached is not None:
            self._attachment_paths_cache.move_to_end(cache_key)
            return cached
        await cur.execute(
            "SELECT m.template, m.path, mo.body FROM state_attachments m "
            "JOIN state_templates mo ON mo.name = m.template WHERE m.state = %s",
            (state,),
        )
        attachment_rows = list(await cur.fetchall())
        composed = (_abs_regime_paths(attachment_rows), _traced_paths(attachment_rows))
        self._attachment_paths_cache[cache_key] = composed
        self._attachment_paths_cache.move_to_end(cache_key)
        if len(self._attachment_paths_cache) > _ATTACHMENT_PATHS_CACHE_MAX:
            self._attachment_paths_cache.popitem(last=False)
        return composed

    async def apply_ops(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: CompletedOrigin,
        validate_doc: Any,
        validate_subject_in_txn: Any,
        conn: AsyncConnection[Any] | None = None,
    ) -> tuple[bool, dict[str, Any] | None, float | None, list[dict[str, Any]]]:
        """Apply a batch of path-addressed ops to one record, in ONE txn.

        Returns ``(applied, merged_document, seq, guarded_skipped)`` —
        ``(False, None, None, [])`` on an op-id replay.

        Order (pinned): the declaration row ``FOR SHARE`` — the schema-change serialization pin,
        the effective-schema read, the declared ``subject_kinds`` and the ``updated_at`` version;
        ``validate_subject_in_txn`` refuses the subject under that lock; compose the state's regime
        + traced paths from its attachments, served from the version-gated cache (a hit on
        ``(state, updated_at)`` skips the attachments JOIN, a miss reads and populates); refuse a
        ``composing`` shape violation BEFORE the op-ledger insert; the op-ledger
        ``INSERT ... ON CONFLICT DO NOTHING`` when ``op_id`` is set (replay ⇒ return without
        touching the record); the ATOMIC UPSERT-LOCK on the record row; the COMPARE-AND-SET GUARD
        filter; the ``_trace`` stamp under a traced attach; the SHARED pure ops apply; the
        whole-document validation; the UPDATE + ``state_writes`` row as ONE writable CTE. With
        ``conn`` the write joins the caller's transaction (a reconciler's record write commits or
        rolls back with the attach).
        """
        async with self._write_cursor(conn) as cur:
            await cur.execute(
                "SELECT effective_schema, subject_kinds, updated_at FROM state_declarations WHERE name = %s FOR SHARE",
                (state,),
            )
            decl = await cur.fetchone()
            if decl is None:
                raise StateNotFoundError(f"no state declared as {state!r}")

            # Refuse the subject inside this transaction, under the declaration lock: the one
            # locked read of the declaration serves the subject admission check AND the
            # effective-schema validation; the door does not separately pre-read the declaration.
            await validate_subject_in_txn(decl["subject_kinds"])

            regime_paths, traced_paths = await self._composed_attachment_paths(cur, state, decl["updated_at"])

            # (i) refuse a composing shape violation BEFORE the ledger insert.
            _refuse_composing_shape(ops, regime_paths)

            kind, key = await self._resolve_subject(cur, state, subject)

            if op_id is not None:
                await cur.execute(
                    "INSERT INTO state_applied_ops (op_id, applied_at) VALUES (%s, now()) ON CONFLICT DO NOTHING",
                    (op_id,),
                )
                if cur.rowcount == 0:
                    return (False, None, None, [])

            await cur.execute(
                "INSERT INTO state_records (state, target_kind, target_name, subject_kind, subject_key, data, "
                "updated_at) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, clock_timestamp()) "
                "ON CONFLICT (state, target_kind, target_name, subject_kind, subject_key) DO UPDATE SET "
                "state = EXCLUDED.state "
                "RETURNING data, extract(epoch FROM updated_at)::float8 AS seq, (xmax = 0) AS inserted",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            record = await cur.fetchone()
            if record is None:
                raise AssertionError
            current = record["data"]

            applied_ops: list[dict[str, Any]] = []
            guarded_skipped: list[dict[str, Any]] = []
            for op in ops:
                guard = op.get("guard")
                if guard is not None and not guard_passes(current, guard):
                    guarded_skipped.append(op)
                    continue
                applied_ops.append({k: v for k, v in op.items() if k != "guard"})

            if not applied_ops:
                if record["inserted"]:
                    await cur.execute(
                        "DELETE FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                        "AND subject_kind = %s AND subject_key = %s",
                        (state, subject.target_kind, subject.target_name, kind, key),
                    )
                    merged: dict[str, Any] | None = None
                    seq: float | None = None
                else:
                    merged = current
                    seq = record["seq"]
                return (True, merged, seq, guarded_skipped)

            # (ii) stamp ``_trace`` under a traced attach, before the apply + validation.
            if traced_paths:
                stamp = {
                    "meta": origin.meta,
                    "run": origin.run_id,
                    "turn": origin.turn_id,
                    "inbound": origin.inbound_id,
                    "at": _iso_now(),
                }
                stamp_trace(applied_ops, traced_paths, stamp)

            merged = apply_path_ops(current, applied_ops)
            validate_doc(decl["effective_schema"], merged)

            # (iii) the record UPDATE and its ``state_writes`` provenance row (touched paths =
            # each applied op's absolute path) land in ONE writable CTE: the UPDATE's returned
            # ``seq`` feeds the write row and comes back to the caller, so the two statements are
            # one round-trip.
            paths = [list(op.get("path") or []) for op in applied_ops]
            await cur.execute(
                "WITH upd AS ("
                "UPDATE state_records SET data = %s, updated_at = clock_timestamp() "
                "WHERE state = %s AND target_kind = %s AND target_name = %s "
                "AND subject_kind = %s AND subject_key = %s "
                "RETURNING extract(epoch FROM updated_at)::float8 AS seq"
                "), w AS ("
                "INSERT INTO state_writes "
                "(state, target_kind, target_name, subject_kind, subject_key, seq, at, door, actor, consumer, "
                "meta, run_id, turn_id, paths, op_id) "
                "SELECT %s, %s, %s, %s, %s, (SELECT seq FROM upd), now(), %s, %s, %s, %s, %s, %s, %s, %s"
                ") SELECT seq FROM upd",
                (
                    Jsonb(merged),
                    state,
                    subject.target_kind,
                    subject.target_name,
                    kind,
                    key,
                    state,
                    subject.target_kind,
                    subject.target_name,
                    kind,
                    key,
                    origin.door,
                    origin.actor,
                    origin.consumer,
                    Jsonb(origin.meta) if origin.meta is not None else None,
                    origin.run_id,
                    origin.turn_id,
                    Jsonb(paths),
                    op_id,
                ),
            )
            seq_row = await cur.fetchone()
            seq = None if seq_row is None else seq_row["seq"]
            return (True, merged, seq, guarded_skipped)

    async def erase_subject(self, state: str, subject: StateSubject, *, origin: CompletedOrigin) -> None:
        """The RTBF delete — idempotent, ALIAS-AWARE, and audited.

        The key resolves to its canonical, the surviving record dies with every
        alias pointing at it, and the erase is recorded (paths ``[[]]``). ONE txn
        under the declaration ``FOR SHARE`` lock; an undeclared state falls through
        to a plain delete.
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR SHARE", (state,))
            declared = await cur.fetchone() is not None
            if not declared:
                await cur.execute(
                    "DELETE FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                    "AND subject_kind = %s AND subject_key = %s",
                    (state, *_subject_cols(subject)),
                )
                return
            kind, key = await self._resolve_subject(cur, state, subject)
            await cur.execute(
                "DELETE FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                "AND subject_kind = %s AND subject_key = %s",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            deleted = cur.rowcount
            await cur.execute(
                "DELETE FROM state_subject_aliases WHERE state = %s AND target_kind = %s AND target_name = %s "
                "AND canonical_kind = %s AND canonical_key = %s",
                (state, subject.target_kind, subject.target_name, kind, key),
            )
            if deleted:
                await self._insert_write(
                    cur, state, subject.target_kind, subject.target_name, kind, key, None, origin, [[]], None
                )

    async def fold_subject(
        self,
        state: str,
        subject: StateSubject,
        into: StateSubject,
        mode: str,
        *,
        origin: CompletedOrigin,
        validate_doc: Any,
    ) -> dict[str, Any]:
        """Fold ``subject`` into ``into`` in ONE txn under the declaration row ``FOR UPDATE``.

        Every ``apply_ops``'s ``FOR SHARE`` blocks, so no write lands mid-fold.
        Both keys resolve first. ``switch`` drops the subject's record; ``merge``
        folds it into the survivor (survivor wins). Refusals raise
        :class:`SubjectFoldError`; a retried fold is a quiet no-op. Records one
        write row for the survivor.
        """
        if subject.target_kind != into.target_kind or subject.target_name != into.target_name:
            raise SubjectFoldError(
                f"cannot fold across targets: {subject.target_kind}/{subject.target_name} into "
                f"{into.target_kind}/{into.target_name}"
            )
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT effective_schema FROM state_declarations WHERE name = %s FOR UPDATE", (state,))
            decl = await cur.fetchone()
            if decl is None:
                raise StateNotFoundError(f"no state declared as {state!r}")

            tk, tn = subject.target_kind, subject.target_name
            await cur.execute(
                "SELECT canonical_kind, canonical_key FROM state_subject_aliases "
                "WHERE state = %s AND target_kind = %s AND target_name = %s AND alias_kind = %s AND alias_key = %s",
                (state, tk, tn, subject.kind, subject.key),
            )
            existing = await cur.fetchone()
            target_kind, target_key = await self._resolve_subject(cur, state, into)

            from_view = {"kind": subject.kind, "key": subject.key}
            into_view = {"kind": target_kind, "key": target_key}
            if existing is not None:
                if existing["canonical_kind"] == target_kind and existing["canonical_key"] == target_key:
                    return {"mode": mode, "from": from_view, "into": into_view, "already": True, "flattened": 0}
                raise SubjectFoldError(
                    f"subject {subject.kind}/{subject.key} of state {state!r} is already folded into "
                    f"{existing['canonical_kind']}/{existing['canonical_key']}; folding it into "
                    f"{target_kind}/{target_key} would fork its identity"
                )
            if target_kind == subject.kind and target_key == subject.key:
                raise SubjectFoldError(
                    f"cannot fold subject {subject.kind}/{subject.key} of state {state!r} into itself"
                )

            await cur.execute(
                "SELECT data FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                "AND subject_kind = %s AND subject_key = %s",
                (state, tk, tn, subject.kind, subject.key),
            )
            src_row = await cur.fetchone()

            report: dict[str, Any] = {"mode": mode, "from": from_view, "into": into_view, "already": False}
            survivor_seq: float | None = None
            if mode == "merge" and src_row is not None:
                await cur.execute(
                    "SELECT data FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                    "AND subject_kind = %s AND subject_key = %s",
                    (state, tk, tn, target_kind, target_key),
                )
                dst_row = await cur.fetchone()
                dst_data: dict[str, Any] = {} if dst_row is None else dst_row["data"]
                merged = {**src_row["data"], **dst_data}  # survivor wins; old fills absent members
                try:
                    validate_doc(decl["effective_schema"], merged)
                except Exception as exc:
                    raise SubjectFoldError(
                        f"merging subject {subject.kind}/{subject.key} into {target_kind}/{target_key} would leave an "
                        f"invalid document: {exc}"
                    ) from exc
                report["merged_members"] = sorted(k for k in src_row["data"] if k not in dst_data)
                await cur.execute(
                    "INSERT INTO state_records (state, target_kind, target_name, subject_kind, subject_key, data, "
                    "updated_at) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp()) "
                    "ON CONFLICT (state, target_kind, target_name, subject_kind, subject_key) DO UPDATE SET "
                    "data = EXCLUDED.data, updated_at = clock_timestamp() "
                    "RETURNING extract(epoch FROM updated_at)::float8 AS seq",
                    (state, tk, tn, target_kind, target_key, Jsonb(merged)),
                )
                seq_row = await cur.fetchone()
                survivor_seq = None if seq_row is None else seq_row["seq"]
            elif mode == "merge":
                report["merged_members"] = []

            if src_row is not None:
                await cur.execute(
                    "DELETE FROM state_records WHERE state = %s AND target_kind = %s AND target_name = %s "
                    "AND subject_kind = %s AND subject_key = %s",
                    (state, tk, tn, subject.kind, subject.key),
                )
            await cur.execute(
                "INSERT INTO state_subject_aliases (state, target_kind, target_name, alias_kind, alias_key, "
                "canonical_kind, canonical_key, mode) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (state, tk, tn, subject.kind, subject.key, target_kind, target_key, mode),
            )
            await cur.execute(
                "UPDATE state_subject_aliases SET canonical_kind = %s, canonical_key = %s "
                "WHERE state = %s AND target_kind = %s AND target_name = %s AND canonical_kind = %s "
                "AND canonical_key = %s",
                (target_kind, target_key, state, tk, tn, subject.kind, subject.key),
            )
            report["flattened"] = cur.rowcount
            await self._insert_write(cur, state, tk, tn, target_kind, target_key, survivor_seq, origin, [[]], None)
            return report
