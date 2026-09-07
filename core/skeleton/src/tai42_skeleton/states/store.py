"""The thin Postgres seam over the kit DB registry (component ``states``).

ALL SQL lives here; service and route code never write SQL. The subject-keyed record
substrate: ``state_declarations`` (base ``schema`` + composed ``effective_schema`` +
the ``subject_kinds`` and ``default_subject_kind`` it serves), ``state_records``,
``state_applied_ops`` (the idempotency ledger), ``state_subject_aliases``,
``state_modules``, ``state_mounts``, and ``state_writes`` (the write provenance
ledger). Connection settings resolve FRESH per operation (``component_store_settings``)
so a config reload re-targets the store.

A SUBJECT is ``(target_kind, target_name, kind, key)`` — four columns everywhere;
equality (and identity across every method) is all four under ``state``. The store
takes a full :class:`~tai42_contract.states.StateSubject` and refuses anything less;
subject resolution belongs to the doors, never here.

SUBJECT ALIASES: a fold leaves ONE live record and an alias row, and every record
access resolves the subject through the alias table INSIDE its own transaction, so an
old key keeps landing on the surviving record. Resolution is ONE hop by invariant:
``fold`` flattens every alias that pointed at the folded subject onto the new
canonical. Folds serialize against writes on the declaration row (``FOR UPDATE`` vs
``apply_ops``'s ``FOR SHARE``). Aliases are IDENTITY, not data: the retention sweep
never touches them; an erase deletes the surviving record AND every alias pointing at
it; a declaration delete cascades them.

Writes stamp ``updated_at = clock_timestamp()`` (statement time, taken while the row
lock is HELD, so it is commit-ordered per record) and return it as ``seq`` — the
channel ordering key. Every record-changing door records one ``state_writes`` row in
the same transaction with the write's COMPLETED origin (``door``/``actor`` stamped by
the platform chokepoint) and the absolute paths it touched — the audit of who wrote
what is never optional.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tai42_contract.states.errors import (
    RegimeViolationError,
    StateNotFoundError,
    StatesError,
    SubjectFoldError,
    ValueValidationError,
)
from tai42_contract.states.models import (
    MAX_RETENTION_DAYS,
    CompletedOrigin,
    StateSubject,
)
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings

from tai42_skeleton.states.db import STATES_COMPONENT, states_settings
from tai42_skeleton.states.modules import path_overlaps
from tai42_skeleton.states.paths import APPEND, KEYED_OPS, guard_passes
from tai42_skeleton.states.paths import apply_ops as apply_path_ops


def _settings() -> Any:
    """The bound store's runtime connection settings, resolved fresh per call."""
    return component_store_settings(STATES_COMPONENT)


def _subject_cols(subject: StateSubject) -> tuple[str, str, str, str]:
    """A subject's four addressing columns ``(target_kind, target_name, kind, key)``."""
    return (subject.target_kind, subject.target_name, subject.kind, subject.key)


def _iso_now() -> str:
    """An ISO-8601 UTC timestamp for a ``_trace`` stamp."""
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# Regime shape refusal + trace stamping (pure)                                  #
# --------------------------------------------------------------------------- #
def _abs_regime_paths(mount_rows: list[dict[str, Any]]) -> list[tuple[list[Any], str, str]]:
    """The absolute regime paths every mount on a state declares, from the stored module
    bodies: for each mount ``(module at base_path)`` and each of the module's regime
    rules, ``base_path + rule.path`` (wildcards preserved), its regime, and the module
    name — the input the composing shape refusal walks. Reads the stored body directly
    (validated at store time), so the hot write path never re-validates a module."""
    out: list[tuple[list[Any], str, str]] = []
    for row in mount_rows:
        base_path = list(row["path"] or [])
        body = row["body"] or {}
        for rule in body.get("regimes", []) or []:
            out.append(([*base_path, *rule.get("path", [])], rule.get("regime"), body.get("name", row["module"])))
    return out


def _traced_paths(mount_rows: list[dict[str, Any]]) -> tuple[tuple[str | int, ...], ...]:
    """The mount paths whose module traces (``trace.enabled``) — the prefixes under which
    a write stamps ``_trace``."""
    paths: list[tuple[str | int, ...]] = []
    for row in mount_rows:
        body = row["body"] or {}
        if bool((body.get("trace") or {}).get("enabled")):
            paths.append(tuple(row["path"] or []))
    return tuple(paths)


def _refuse_composing_shape(ops: list[dict[str, Any]], regime_paths: list[tuple[list[Any], str, str]]) -> None:
    """Refuse a write whose SHAPE violates a ``composing`` path (D-4): a whole-path
    ``set``/``remove`` over a composing path admits only a keyed op or an append ``set``
    (path ending ``"-"``). Anything else raises :class:`RegimeViolationError` naming the
    path — BEFORE the ledger insert, so a refused batch consumes no op-id."""
    for op in ops:
        path = op.get("path")
        if not isinstance(path, list):
            continue
        kind = op.get("op")
        append_set = kind == "set" and bool(path) and path[-1] == APPEND
        if kind in KEYED_OPS or append_set:
            continue
        if kind not in ("set", "remove"):
            continue
        for abs_pattern, regime, module_name in regime_paths:
            if regime == "composing" and path_overlaps(path, abs_pattern):
                raise RegimeViolationError(
                    f"composing path {abs_pattern} of module {module_name!r} admits only keyed ops or an append "
                    f"set (path ending '-'); this write uses {kind!r} at {path}"
                )


def _under_traced_path(path: list[Any], traced_paths: tuple[tuple[str | int, ...], ...]) -> bool:
    """Whether ``path`` lies at or under any tracing mount path (the mount path is a
    prefix of the op path — the op writes into the mounted subtree)."""
    for mount_path in traced_paths:
        if len(mount_path) <= len(path) and all(mount_path[i] == path[i] for i in range(len(mount_path))):
            return True
    return False


def _stamp_items(value: Any, stamp: dict[str, Any]) -> None:
    """Stamp ``_trace`` into ``value`` when it is an object, or into each object item when
    it is a list; non-objects are untouched."""
    if isinstance(value, dict):
        value["_trace"] = dict(stamp)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                item["_trace"] = dict(stamp)


def stamp_trace(
    ops: list[dict[str, Any]], traced_paths: tuple[tuple[str | int, ...], ...], stamp: dict[str, Any]
) -> None:
    """Stamp ``_trace`` into every object an op WRITES, for each op whose absolute path
    lies under a tracing mount path. Mutates the ops in place before the apply, so the
    effective schema's ``_trace`` property admits the stamped field. Keyed ops carry an
    item object or a list of them; ``set_by_key_each`` carries a ``{key: [items…]}``
    fan-out; a ``set`` (including a ``"-"`` append) carries the object value. Non-object
    values — scalars, arrays of scalars, remove keys — are untouched."""
    for op in ops:
        path = op.get("path")
        if not isinstance(path, list) or not _under_traced_path(path, traced_paths):
            continue
        kind = op.get("op")
        if kind == "set_by_key_each":
            value = op.get("value")
            if isinstance(value, dict):
                for items in value.values():
                    _stamp_items(items, stamp)
        elif kind in ("set_by_key", "merge_by_key"):
            _stamp_items(op.get("value"), stamp)
        elif kind == "set":
            value = op.get("value")
            if isinstance(value, dict):
                value["_trace"] = dict(stamp)


class PostgresStatesStore:
    """One class over the record substrate's tables. Each method opens its own pooled
    connection; multi-statement operations run in one explicit transaction."""

    # -- declarations ------------------------------------------------------------

    async def get_declaration(self, name: str) -> dict[str, Any] | None:
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT name, description, schema, effective_schema, subject_kinds, default_subject_kind, "
                "retention_days, updated_at FROM state_declarations WHERE name = %s",
                (name,),
            )
            return await cur.fetchone()

    async def list_declarations(self) -> list[dict[str, Any]]:
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT name, description, schema, effective_schema, subject_kinds, default_subject_kind, "
                "retention_days, updated_at FROM state_declarations ORDER BY name"
            )
            return list(await cur.fetchall())

    async def upsert_declaration(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        subject_kinds: list[str],
        default_subject_kind: str,
        retention_days: int | None,
        effective_schema: dict[str, Any] | None = None,
    ) -> None:
        """The bare declaration upsert — no guard. Service-level writes go through
        :meth:`upsert_declaration_guarded`; this raw op backs tests only.

        ``effective_schema`` defaults to ``schema`` (an unmounted state's effective
        schema IS its base), so a caller that never touches modules stays correct."""
        effective = schema if effective_schema is None else effective_schema
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "INSERT INTO state_declarations "
                "(name, description, schema, effective_schema, subject_kinds, default_subject_kind, "
                "retention_days, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description, "
                "schema = EXCLUDED.schema, effective_schema = EXCLUDED.effective_schema, "
                "subject_kinds = EXCLUDED.subject_kinds, default_subject_kind = EXCLUDED.default_subject_kind, "
                "retention_days = EXCLUDED.retention_days, updated_at = now()",
                (
                    name,
                    description,
                    Jsonb(schema),
                    Jsonb(effective),
                    Jsonb(list(subject_kinds)),
                    default_subject_kind,
                    retention_days,
                ),
            )

    async def upsert_declaration_guarded(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        subject_kinds: list[str],
        default_subject_kind: str,
        retention_days: int | None,
        *,
        effective_schema: dict[str, Any],
        decide: Any,
    ) -> None:
        """Guarded upsert, closing the narrowing check-then-act race in ONE txn.

        Locks the declaration row ``FOR UPDATE`` (blocking every ``apply_ops``'s
        ``FOR SHARE``, so no record can land mid-guard), reads the existing row and the
        per-kind record counts under that lock, then calls ``decide(existing_row |
        None, per_kind_counts)`` — which raises to refuse (aborting the txn) — and
        finally performs the upsert."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT schema, subject_kinds, default_subject_kind FROM state_declarations WHERE name = %s FOR UPDATE",
                (name,),
            )
            existing = await cur.fetchone()
            per_kind: dict[str, int] = {}
            if existing is not None:
                await cur.execute(
                    "SELECT subject_kind, count(*) AS n FROM state_records WHERE state = %s GROUP BY subject_kind",
                    (name,),
                )
                per_kind = {row["subject_kind"]: int(row["n"]) for row in await cur.fetchall()}
            decide(existing, per_kind)  # raises to refuse — the txn aborts
            await cur.execute(
                "INSERT INTO state_declarations "
                "(name, description, schema, effective_schema, subject_kinds, default_subject_kind, "
                "retention_days, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description, "
                "schema = EXCLUDED.schema, effective_schema = EXCLUDED.effective_schema, "
                "subject_kinds = EXCLUDED.subject_kinds, default_subject_kind = EXCLUDED.default_subject_kind, "
                "retention_days = EXCLUDED.retention_days, updated_at = now()",
                (
                    name,
                    description,
                    Jsonb(schema),
                    Jsonb(effective_schema),
                    Jsonb(list(subject_kinds)),
                    default_subject_kind,
                    retention_days,
                ),
            )

    async def delete_declaration(self, name: str) -> bool:
        """Delete a state with its records, mounts, aliases and write ledger in ONE txn
        under the declaration lock. ``False`` when no declaration exists (nothing
        deleted). The consumer-binding refusal is the service's (it consults the
        registered consumer listers before calling this)."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR UPDATE", (name,))
            if await cur.fetchone() is None:
                return False
            await cur.execute("DELETE FROM state_writes WHERE state = %s", (name,))
            await cur.execute("DELETE FROM state_subject_aliases WHERE state = %s", (name,))
            await cur.execute("DELETE FROM state_records WHERE state = %s", (name,))
            await cur.execute("DELETE FROM state_mounts WHERE state = %s", (name,))
            await cur.execute("DELETE FROM state_declarations WHERE name = %s", (name,))
            return True

    async def count_records(self, state: str) -> int:
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT count(*) AS n FROM state_records WHERE state = %s", (state,))
            row = await cur.fetchone()
            return 0 if row is None else int(row["n"])

    async def count_records_for_target(self, target_kind: str, target_name: str) -> int:
        """Records addressed under one conversation target ``(target_kind, target_name)``
        across every state — the rename referee's evidence that renaming a ``tool`` target
        would strand its subject records."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT count(*) AS n FROM state_records WHERE target_kind = %s AND target_name = %s",
                (target_kind, target_name),
            )
            row = await cur.fetchone()
            return 0 if row is None else int(row["n"])

    async def field_stats(self, state: str) -> tuple[int, dict[str, int], dict[str, int]]:
        """``(record_count, per_field, per_kind)`` for the listing/stats dialog. A
        top-level key is present in ``data`` only while it holds data, so a per-key count
        IS the count of records holding that field; ``per_kind`` counts records by
        subject kind."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT count(*) AS n FROM state_records WHERE state = %s", (state,))
            count_row = await cur.fetchone()
            records = 0 if count_row is None else int(count_row["n"])
            await cur.execute(
                "SELECT key, count(*) AS n FROM state_records, jsonb_object_keys(data) AS key "
                "WHERE state = %s GROUP BY key",
                (state,),
            )
            per_field = {row["key"]: int(row["n"]) for row in await cur.fetchall()}
            await cur.execute(
                "SELECT subject_kind, count(*) AS n FROM state_records WHERE state = %s GROUP BY subject_kind",
                (state,),
            )
            per_kind = {row["subject_kind"]: int(row["n"]) for row in await cur.fetchall()}
            return records, per_field, per_kind

    # -- modules -----------------------------------------------------------------

    async def get_module(self, name: str) -> dict[str, Any] | None:
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT name, body, shipped_hash, updated_at FROM state_modules WHERE name = %s",
                (name,),
            )
            return await cur.fetchone()

    async def list_modules(self) -> list[dict[str, Any]]:
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name, body, shipped_hash, updated_at FROM state_modules ORDER BY name")
            return list(await cur.fetchall())

    async def mounted_module_counts(self) -> dict[str, int]:
        """The number of states each module is mounted on, keyed by module name — one
        aggregate over the mounts table for the whole catalog. A module with no mount is
        absent from the map (the caller reads a missing key as zero)."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT module, count(*) AS n FROM state_mounts GROUP BY module")
            return {row["module"]: int(row["n"]) for row in await cur.fetchall()}

    async def upsert_module(self, name: str, body: dict[str, Any], shipped_hash: str | None) -> None:
        """Write a module document. ``shipped_hash`` is the seed applier's canonical-body
        hash on a shipped default (NULL for an operator upload); it is the only field the
        applier uses to tell an unedited shipped module from an operator-owned one."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "INSERT INTO state_modules (name, body, shipped_hash, updated_at) VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (name) DO UPDATE SET body = EXCLUDED.body, "
                "shipped_hash = EXCLUDED.shipped_hash, updated_at = now()",
                (name, Jsonb(body), shipped_hash),
            )

    async def delete_module(self, name: str) -> bool:
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("DELETE FROM state_modules WHERE name = %s", (name,))
            return cur.rowcount > 0

    # -- mounts ------------------------------------------------------------------

    async def get_mount(self, state: str, module: str) -> dict[str, Any] | None:
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT state, module, path, parameters, declarations, updated_at "
                "FROM state_mounts WHERE state = %s AND module = %s",
                (state, module),
            )
            return await cur.fetchone()

    async def list_mounts_for_state(self, state: str) -> list[dict[str, Any]]:
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT state, module, path, parameters, declarations, updated_at "
                "FROM state_mounts WHERE state = %s ORDER BY module",
                (state,),
            )
            return list(await cur.fetchall())

    async def list_mounts_of_module(self, module: str) -> list[dict[str, Any]]:
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT state, module, path, parameters, declarations, updated_at "
                "FROM state_mounts WHERE module = %s ORDER BY state",
                (module,),
            )
            return list(await cur.fetchall())

    async def list_all_mounts(self) -> list[dict[str, Any]]:
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT state, module, path, parameters, declarations, updated_at "
                "FROM state_mounts ORDER BY state, module"
            )
            return list(await cur.fetchall())

    async def upsert_mount(
        self,
        state: str,
        module: str,
        path: list[str],
        parameters: dict[str, Any],
        declarations: dict[str, Any],
        *,
        effective_schema: dict[str, Any],
    ) -> None:
        """Write a mount row and the state's recomposed effective schema in ONE txn, under
        the declaration lock (serializing against every migrate, so the effective schema a
        concurrent write validates against is never half-composed). Refuses loudly when
        the state is not declared."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR UPDATE", (state,))
            if await cur.fetchone() is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            await cur.execute(
                "INSERT INTO state_mounts (state, module, path, parameters, declarations, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (state, module) DO UPDATE SET path = EXCLUDED.path, "
                "parameters = EXCLUDED.parameters, declarations = EXCLUDED.declarations, updated_at = now()",
                (state, module, Jsonb(path), Jsonb(parameters), Jsonb(declarations)),
            )
            await cur.execute(
                "UPDATE state_declarations SET effective_schema = %s, updated_at = now() WHERE name = %s",
                (Jsonb(effective_schema), state),
            )

    async def update_mount_declarations(
        self, state: str, module: str, declarations: dict[str, Any], *, effective_schema: dict[str, Any]
    ) -> bool:
        """Rewrite a mount's declarations (values only) and the state's effective schema in
        ONE txn under the declaration lock. ``False`` when no such mount exists."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR UPDATE", (state,))
            if await cur.fetchone() is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            await cur.execute(
                "UPDATE state_mounts SET declarations = %s, updated_at = now() WHERE state = %s AND module = %s",
                (Jsonb(declarations), state, module),
            )
            if cur.rowcount == 0:
                return False
            await cur.execute(
                "UPDATE state_declarations SET effective_schema = %s, updated_at = now() WHERE name = %s",
                (Jsonb(effective_schema), state),
            )
            return True

    async def update_mount_parameters(
        self, state: str, module: str, parameters: dict[str, Any], *, effective_schema: dict[str, Any]
    ) -> bool:
        """Rewrite a mount's stored (effective) parameters and the state's effective schema
        in ONE txn under the declaration lock — used by a module replace to backfill a
        newly defaulted parameter into a live mount. ``False`` when no such mount exists."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR UPDATE", (state,))
            if await cur.fetchone() is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            await cur.execute(
                "UPDATE state_mounts SET parameters = %s, updated_at = now() WHERE state = %s AND module = %s",
                (Jsonb(parameters), state, module),
            )
            if cur.rowcount == 0:
                return False
            await cur.execute(
                "UPDATE state_declarations SET effective_schema = %s, updated_at = now() WHERE name = %s",
                (Jsonb(effective_schema), state),
            )
            return True

    async def delete_mount(self, state: str, module: str, *, effective_schema: dict[str, Any]) -> bool:
        """Delete a mount row and rewrite the state's effective schema — in ONE txn under
        the declaration lock. ``False`` when no such mount exists. A consumer's bindings
        are DERIVED (never stored), so a mount delete cleans up nothing else (D-5)."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT name FROM state_declarations WHERE name = %s FOR UPDATE", (state,))
            if await cur.fetchone() is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            await cur.execute("DELETE FROM state_mounts WHERE state = %s AND module = %s", (state, module))
            if cur.rowcount == 0:
                return False
            await cur.execute(
                "UPDATE state_declarations SET effective_schema = %s, updated_at = now() WHERE name = %s",
                (Jsonb(effective_schema), state),
            )
            return True

    # -- records: alias resolution ----------------------------------------------

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

    # -- records: reads ----------------------------------------------------------

    async def read_record(self, state: str, subject: StateSubject) -> tuple[dict[str, Any] | None, float | None]:
        """``(data, seq)`` for ``subject`` — the subject resolves through the alias table
        (a folded key lands on the surviving record); ``(None, None)`` when absent."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
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

    async def read_record_view(self, state: str, subject: StateSubject) -> dict[str, Any] | None:
        """The read door's view: ``{data, seq, canonical_subject, folded_from}`` —
        ``folded_from`` is every alias pointing at the canonical — or ``None`` when no
        record exists."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
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

    async def all_records(self, state: str) -> dict[tuple[str, str, str, str], dict[str, Any]]:
        """Every ``{(target_kind, target_name, subject_kind, subject_key): data}`` of a
        state — the migrate-PREVIEW read. A lock-free snapshot (a preview is advisory)."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT target_kind, target_name, subject_kind, subject_key, data FROM state_records "
                "WHERE state = %s ORDER BY target_kind, target_name, subject_kind, subject_key",
                (state,),
            )
            return {
                (r["target_kind"], r["target_name"], r["subject_kind"], r["subject_key"]): r["data"]
                for r in await cur.fetchall()
            }

    async def export_records(self, state: str) -> list[dict[str, Any]]:
        """Every record of a state as export rows ``{target_kind, target_name,
        subject_kind, subject_key, data}`` — the backup exporter's read."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
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
            client_ctx(PostgresClient, _settings()) as pool,
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

    # -- records: writes ---------------------------------------------------------

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
        """Record one ``state_writes`` row — the completed origin plus the touched paths —
        in the caller's transaction."""
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
        """Replace ``subject``'s whole document with ``data`` (validated whole against the
        effective schema) and record the write (paths ``[[]]`` — the whole document). ONE
        txn under the declaration ``FOR SHARE`` lock; the subject resolves through the
        alias table first."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
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

    async def apply_ops(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: CompletedOrigin,
        validate_doc: Any,
        retention_days: int,
    ) -> tuple[bool, dict[str, Any] | None, float | None, list[dict[str, Any]]]:
        """Apply a batch of path-addressed ops to one record, in ONE txn. Returns
        ``(applied, merged_document, seq, guarded_skipped)`` — ``(False, None, None, [])``
        on an op-id replay.

        Order (pinned): declaration row ``FOR SHARE`` (the migrate serialization pin AND
        the effective-schema read); compose the state's regime + traced paths from its
        mounts under the lock; refuse a ``composing`` shape violation BEFORE the op-ledger
        insert (D-4); the op-ledger ``INSERT ... ON CONFLICT DO NOTHING`` when ``op_id`` is
        set (replay ⇒ return without touching the record); the ATOMIC UPSERT-LOCK on the
        record row; the COMPARE-AND-SET GUARD filter; the ``_trace`` stamp under a traced
        mount (D-3); the SHARED pure ops apply; the whole-document validation; the UPDATE;
        the ``state_writes`` row; opportunistic ledger prune.
        """
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT effective_schema FROM state_declarations WHERE name = %s FOR SHARE", (state,))
            decl = await cur.fetchone()
            if decl is None:
                raise StateNotFoundError(f"no state declared as {state!r}")

            # Compose the regime + traced paths from the state's mounts under the lock, so
            # the shape refusal and the trace stamp read the mounts committed at write time.
            await cur.execute(
                "SELECT m.module, m.path, mo.body FROM state_mounts m "
                "JOIN state_modules mo ON mo.name = m.module WHERE m.state = %s",
                (state,),
            )
            mount_rows = list(await cur.fetchall())
            regime_paths = _abs_regime_paths(mount_rows)
            traced_paths = _traced_paths(mount_rows)

            # (i) refuse a composing shape violation BEFORE the ledger insert (D-4).
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
            assert record is not None  # the upsert-lock returns exactly one row, always
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
                await cur.execute(
                    "DELETE FROM state_applied_ops WHERE applied_at < now() - make_interval(days => %s)",
                    (retention_days,),
                )
                return (True, merged, seq, guarded_skipped)

            # (ii) stamp ``_trace`` under a traced mount, before the apply + validation.
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

            await cur.execute(
                "UPDATE state_records SET data = %s, updated_at = clock_timestamp() "
                "WHERE state = %s AND target_kind = %s AND target_name = %s AND subject_kind = %s AND subject_key = %s "
                "RETURNING extract(epoch FROM updated_at)::float8 AS seq",
                (Jsonb(merged), state, subject.target_kind, subject.target_name, kind, key),
            )
            seq_row = await cur.fetchone()
            seq = None if seq_row is None else seq_row["seq"]
            # (iii) one write row with the touched paths (each applied op's absolute path).
            paths = [list(op.get("path") or []) for op in applied_ops]
            await self._insert_write(
                cur, state, subject.target_kind, subject.target_name, kind, key, seq, origin, paths, op_id
            )
            await cur.execute(
                "DELETE FROM state_applied_ops WHERE applied_at < now() - make_interval(days => %s)",
                (retention_days,),
            )
            return (True, merged, seq, guarded_skipped)

    async def erase_subject(self, state: str, subject: StateSubject, *, origin: CompletedOrigin) -> None:
        """The RTBF delete — idempotent, ALIAS-AWARE, and audited. The key resolves to its
        canonical, the surviving record dies with every alias pointing at it, and the erase
        is recorded (paths ``[[]]``). ONE txn under the declaration ``FOR SHARE`` lock; an
        undeclared state falls through to a plain delete."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
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
        """Fold ``subject`` into ``into`` — ONE txn under the declaration row ``FOR
        UPDATE`` (every ``apply_ops``'s ``FOR SHARE`` blocks, so no write lands mid-fold).
        Both keys resolve first. ``switch`` drops the subject's record; ``merge`` folds it
        into the survivor (survivor wins). Refusals raise :class:`SubjectFoldError`; a
        retried fold is a quiet no-op. Records one write row for the survivor."""
        if subject.target_kind != into.target_kind or subject.target_name != into.target_name:
            raise SubjectFoldError(
                f"cannot fold across targets: {subject.target_kind}/{subject.target_name} into "
                f"{into.target_kind}/{into.target_name}"
            )
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
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

    async def list_subjects(
        self, state: str, *, kind: str | None, limit: int, cursor: str | None
    ) -> list[dict[str, Any]]:
        """One keyset page of a state's subjects, ordered by the FULL subject identity
        ``(target_kind, target_name, subject_kind, subject_key)`` (optionally one
        ``kind``), each row ``{target_kind, target_name, kind, key, updated_at}``, starting
        strictly after ``cursor`` (the packed identity of the last row seen)."""
        after = ("", "", "", "") if cursor is None else _split_cursor(cursor)
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
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
            client_ctx(PostgresClient, _settings()) as pool,
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
            client_ctx(PostgresClient, _settings()) as pool,
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

    async def migrate(
        self,
        state: str,
        new_schema: dict[str, Any],
        new_effective_schema: dict[str, Any],
        *,
        decide: Any,
        origin: CompletedOrigin,
    ) -> None:
        """Replace a declaration's base + effective schema, converting every record
        atomically in ONE txn under the declaration ``FOR UPDATE`` lock. The narrowing
        decision + converter are built from the old BASE schema RE-READ under the lock; a
        failing rule or an invalid converted record aborts the WHOLE change. Records one
        ``state_writes`` row per converted subject (paths ``[[]]`` — the whole document)
        under ``origin``, in the same transaction."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT schema FROM state_declarations WHERE name = %s FOR UPDATE", (state,))
            row = await cur.fetchone()
            if row is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            convert = decide(row["schema"])  # built UNDER the lock; raises to refuse

            await cur.execute(
                "SELECT target_kind, target_name, subject_kind, subject_key, data FROM state_records "
                "WHERE state = %s ORDER BY target_kind, target_name, subject_kind, subject_key FOR UPDATE",
                (state,),
            )
            records = {
                (r["target_kind"], r["target_name"], r["subject_kind"], r["subject_key"]): r["data"]
                for r in await cur.fetchall()
            }
            converted = await convert(records)
            for (tk, tn, sk, key), data in converted.items():
                await cur.execute(
                    "UPDATE state_records SET data = %s, updated_at = clock_timestamp() "
                    "WHERE state = %s AND target_kind = %s AND target_name = %s AND subject_kind = %s "
                    "AND subject_key = %s RETURNING extract(epoch FROM updated_at)::float8 AS seq",
                    (Jsonb(data), state, tk, tn, sk, key),
                )
                seq_row = await cur.fetchone()
                if seq_row is not None:
                    await self._insert_write(cur, state, tk, tn, sk, key, seq_row["seq"], origin, [[]], None)
            await cur.execute(
                "UPDATE state_declarations SET schema = %s, effective_schema = %s, updated_at = clock_timestamp() "
                "WHERE name = %s",
                (Jsonb(new_schema), Jsonb(new_effective_schema), state),
            )

    # -- bulk import (backup restore + the transfer tool) ------------------------

    async def import_records(
        self, state: str, rows: list[dict[str, Any]], *, origin: CompletedOrigin, validate_doc: Any
    ) -> None:
        """Import record rows for ``state`` under the completed origin, validating each
        document against the effective schema, in ONE txn. Each row carries its four
        subject columns plus ``data``. A record already present with equal data is a
        no-op; a differing one is overwritten. Records one write per imported row."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute("SELECT effective_schema FROM state_declarations WHERE name = %s FOR SHARE", (state,))
            decl = await cur.fetchone()
            if decl is None:
                raise StateNotFoundError(f"no state declared as {state!r}")
            for row in rows:
                data = row["data"]
                validate_doc(decl["effective_schema"], data)
                tk, tn = row["target_kind"], row["target_name"]
                kind, key = row["subject_kind"], row["subject_key"]
                await cur.execute(
                    "INSERT INTO state_records (state, target_kind, target_name, subject_kind, subject_key, data, "
                    "updated_at) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp()) "
                    "ON CONFLICT (state, target_kind, target_name, subject_kind, subject_key) DO UPDATE SET "
                    "data = EXCLUDED.data, updated_at = clock_timestamp() "
                    "RETURNING extract(epoch FROM updated_at)::float8 AS seq",
                    (state, tk, tn, kind, key, Jsonb(data)),
                )
                seq_row = await cur.fetchone()
                seq = None if seq_row is None else seq_row["seq"]
                await self._insert_write(cur, state, tk, tn, kind, key, seq, origin, [[]], None)

    async def import_aliases(self, state: str, rows: list[dict[str, Any]]) -> None:
        """Import subject-alias rows for ``state`` verbatim, in ONE txn (identity, not a
        write — no ledger row). Each row carries the target, the alias ``(kind, key)``,
        the canonical ``(kind, key)`` and the ``mode``."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            for row in rows:
                await cur.execute(
                    "INSERT INTO state_subject_aliases (state, target_kind, target_name, alias_kind, alias_key, "
                    "canonical_kind, canonical_key, mode) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (state, target_kind, target_name, alias_kind, alias_key) DO UPDATE SET "
                    "canonical_kind = EXCLUDED.canonical_kind, canonical_key = EXCLUDED.canonical_key, "
                    "mode = EXCLUDED.mode",
                    (
                        state,
                        row["target_kind"],
                        row["target_name"],
                        row["alias_kind"],
                        row["alias_key"],
                        row["canonical_kind"],
                        row["canonical_key"],
                        row["mode"],
                    ),
                )

    async def import_applied_ops(self, rows: list[dict[str, Any]]) -> None:
        """Import applied-op ledger rows verbatim (the idempotency keys carry no subject),
        in ONE txn — a re-imported id is a no-op."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            for row in rows:
                await cur.execute(
                    "INSERT INTO state_applied_ops (op_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    (row["op_id"],),
                )

    # -- retention ---------------------------------------------------------------

    async def prune_ops(self, retention_days: int) -> None:
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "DELETE FROM state_applied_ops WHERE applied_at < now() - make_interval(days => %s)",
                (retention_days,),
            )

    async def prune_expired(self, default_retention_days: int | None) -> dict[str, int]:
        """Delete every record past its state's EFFECTIVE retention (the state's own
        ``retention_days`` when set, else the global default; ``NULL`` keeps records
        forever), in ONE atomic statement. Returns ``{state: rows_deleted}``."""
        async with (
            client_ctx(PostgresClient, _settings()) as pool,
            pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "DELETE FROM state_records r USING state_declarations d WHERE r.state = d.name "
                "AND COALESCE(d.retention_days, %(default)s) IS NOT NULL "
                "AND r.updated_at < now() - make_interval(days => COALESCE(d.retention_days, %(default)s)) "
                "RETURNING r.state",
                {"default": default_retention_days},
            )
            counts: dict[str, int] = {}
            for row in await cur.fetchall():
                counts[row["state"]] = counts.get(row["state"], 0) + 1
            return counts


def _split_cursor(cursor: str) -> tuple[str, str, str, str]:
    """Unpack a ``(target_kind, target_name, subject_kind, subject_key)`` keyset cursor
    packed as ``"<tk>\\x00<tn>\\x00<kind>\\x00<key>"`` — the FULL subject identity, so no
    two rows sharing a ``(subject_kind, subject_key)`` across targets collide at a page
    boundary. A client-supplied cursor that does not carry the four packed parts is a bad
    input (422), never a 500 from unpacking deep in the query."""
    parts = cursor.split("\x00")
    if len(parts) != 4:
        raise ValueValidationError("cursor is malformed; use only a cursor returned by a prior page")
    tk, tn, kind, key = parts
    return tk, tn, kind, key


def make_cursor(target_kind: str, target_name: str, kind: str, key: str) -> str:
    """Pack a ``(target_kind, target_name, subject_kind, subject_key)`` keyset cursor."""
    return f"{target_kind}\x00{target_name}\x00{kind}\x00{key}"


def store_settings_retention() -> int:
    """The op-ledger retention window in days, read fresh and validated LOUDLY: a
    ``0``/negative value would turn the opportunistic prune inside every write into a
    full ledger wipe, so a misconfigured value refuses the write instead."""
    value = states_settings().op_retention_days
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > MAX_RETENTION_DAYS:
        raise StatesError(f"STATES_OP_RETENTION_DAYS must be a positive integer ≤ {MAX_RETENTION_DAYS}, got {value!r}")
    return value


def store_settings_default_retention() -> int | None:
    """The global default RECORD retention window in days, read fresh (``None`` keeps
    records forever unless a state sets its own ``retention_days``)."""
    return states_settings().default_retention_days
