"""Flyway-model SQL migration runner.

A *component* is one named schema chain (the core skeleton, or a table-owning
plugin identified by its distribution name). Its migrations are plain-SQL files
named ``NNNN_short_name.sql`` in a packaged directory, ordered by the zero-padded
numeric prefix. There are no down-migrations: chains roll forward only.

Applying a chain: discover its files, read the versions already recorded in the
per-database ``tai_schema_history`` table, verify every recorded version still
matches the file that produced it (a checksum change on an applied version means
the chain was rewritten — a hard error, never a silent re-run), then run each
pending file in its own transaction, recording its history row in that same
transaction so the file and its record commit or roll back together.

Concurrency and identity invariants:

- The advisory lock that serialises concurrent runners is SESSION-scoped, so the
  runner pins ONE connection for the whole run and executes the lock and every
  per-file transaction on that same held connection. A pool-checkout-per-file
  design would make the lock protect nothing.
- Entries are grouped by resolved DSN: each database gets its own history table
  and its own lock, so a component pointed at a different Postgres (via its own
  ``*_PG_*`` override) is migrated there, not on the default database.

A per-file transaction is the stated v1 limitation: statements that cannot run
inside a transaction (``CREATE INDEX CONCURRENTLY``, some ``ALTER TYPE … ADD
VALUE``) are unsupported.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from itertools import pairwise
from typing import LiteralString, cast

from psycopg import AsyncConnection
from psycopg.errors import UndefinedTable

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.settings import PostgresConnectionSettings

logger = logging.getLogger(__name__)

# One session-scoped advisory lock guards every migration run against a database,
# so two racing replicas serialise (one waits) rather than both applying. The key
# is the 8 ASCII bytes "tai_migr" as a bigint; it is deliberately distinct from
# the marketplace installer's own advisory key so the two never collide when a
# plugin install runs migrations while holding the marketplace lock.
_MIGRATION_LOCK_KEY = 0x7461695F6D696772  # "tai_migr"

# The per-database history table. Created idempotently in the apply path ALONE
# (``apply_migrations``, under the DDL-owning identity and the advisory lock) —
# never by a read-only caller, so ``migration_status`` and the boot gate run under
# a lesser-privileged role that owns neither the table nor CREATE on the schema.
# ``GRANT SELECT … TO PUBLIC`` lets that lesser-privileged connection read chain
# state without the DDL-owning identity — the table holds no secrets.
_HISTORY_DDL: LiteralString = """
CREATE TABLE IF NOT EXISTS tai_schema_history (
    component text NOT NULL,
    version integer NOT NULL,
    name text NOT NULL,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (component, version)
);
GRANT SELECT ON tai_schema_history TO PUBLIC;
"""

# ``NNNN_short_name.sql`` — one or more leading digits, an underscore, a non-empty
# name, the ``.sql`` suffix. A ``.sql`` file that does not match is a loud error,
# never silently skipped.
_FILENAME_RE = re.compile(r"^(\d+)_(.+)\.sql$")


class MigrationError(Exception):
    """Base class for every migration-runner failure."""


class MigrationDiscoveryError(MigrationError):
    """A migrations directory is missing or holds a malformed / duplicate file."""


class ChecksumMismatchError(MigrationError):
    """An already-applied version no longer matches the file that produced it.

    The chain was rewritten (or a recorded file vanished): the applied schema and
    the source have diverged, so continuing could apply the wrong SQL on top of
    the wrong state. Recovery is an explicit operator decision, never automatic.
    """


class SchemaOutOfDateError(MigrationError):
    """A schema-owning component has pending migrations or a checksum mismatch, so a
    boot gate refuses to start the process. Raised by :func:`assert_chain_applied`
    from the non-raising :func:`migration_status` verdict; recovery is the caller's
    supplied remediation (kit hard-codes no CLI verb)."""


@dataclass(frozen=True)
class MigrationScript:
    """One discovered migration file, with its content and checksum."""

    version: int
    name: str
    filename: str
    checksum: str
    sql: str


@dataclass(frozen=True)
class MigrationEntry:
    """A chain to run: a component name, its packaged migrations directory, and
    the DDL-privileged connection settings for the database it targets."""

    component: str
    migrations_dir: Traversable
    settings: PostgresConnectionSettings


@dataclass(frozen=True)
class AppliedMigration:
    """One outcome per file applied during a run."""

    component: str
    version: int
    name: str
    checksum: str


@dataclass(frozen=True)
class ChecksumMismatch:
    """A recorded version whose file changed (``current_checksum`` differs) or
    disappeared (``current_checksum`` is ``None``)."""

    component: str
    version: int
    name: str
    recorded_checksum: str
    current_checksum: str | None


@dataclass(frozen=True)
class ComponentStatus:
    """Non-raising verdict for one component: what is applied, what is pending,
    and any integrity mismatch. Consumed by ``tai db status`` and boot gates."""

    component: str
    applied_versions: tuple[int, ...]
    pending: tuple[MigrationScript, ...]
    mismatches: tuple[ChecksumMismatch, ...]

    @property
    def is_up_to_date(self) -> bool:
        """True only when nothing is pending and no recorded version has diverged."""
        return not self.pending and not self.mismatches


def discover_migrations(migrations_dir: Traversable) -> list[MigrationScript]:
    """Return the chain in ``migrations_dir`` ordered by version.

    Reads every ``*.sql`` file, parses its ``NNNN_short_name`` prefix, and
    computes a SHA-256 of the file bytes. Raises :class:`MigrationDiscoveryError`
    if the directory is absent, a ``.sql`` file is misnamed, or two files claim
    the same version.
    """
    if not migrations_dir.is_dir():
        raise MigrationDiscoveryError(f"migrations directory not found: {migrations_dir}")

    scripts: list[MigrationScript] = []
    for child in migrations_dir.iterdir():
        if not child.is_file() or not child.name.endswith(".sql"):
            continue
        match = _FILENAME_RE.match(child.name)
        if match is None:
            raise MigrationDiscoveryError(
                f"migration file {child.name!r} does not match the required 'NNNN_short_name.sql' form"
            )
        data = child.read_bytes()
        scripts.append(
            MigrationScript(
                version=int(match.group(1)),
                name=match.group(2),
                filename=child.name,
                checksum=hashlib.sha256(data).hexdigest(),
                sql=data.decode("utf-8"),
            )
        )

    scripts.sort(key=lambda s: s.version)
    for earlier, later in pairwise(scripts):
        if earlier.version == later.version:
            raise MigrationDiscoveryError(
                f"duplicate migration version {later.version} ({earlier.filename!r} and {later.filename!r})"
            )
    return scripts


def _reconcile(
    component: str,
    scripts: Sequence[MigrationScript],
    applied: dict[int, tuple[str, str]],
) -> tuple[tuple[MigrationScript, ...], tuple[ChecksumMismatch, ...]]:
    """Split a chain against the recorded history into (pending, mismatches).

    ``applied`` maps a recorded version to its ``(name, checksum)``. A recorded
    version whose current file changed — or is now absent — is a mismatch.
    """
    by_version = {s.version: s for s in scripts}
    mismatches: list[ChecksumMismatch] = []
    for version, (name, checksum) in sorted(applied.items()):
        script = by_version.get(version)
        if script is None:
            mismatches.append(ChecksumMismatch(component, version, name, checksum, None))
        elif script.checksum != checksum:
            mismatches.append(ChecksumMismatch(component, version, name, checksum, script.checksum))
    pending = tuple(s for s in scripts if s.version not in applied)
    return pending, tuple(mismatches)


@asynccontextmanager
async def _pinned_connection(settings: PostgresConnectionSettings) -> AsyncIterator[AsyncConnection]:
    """Yield a single dedicated connection held for a whole run.

    A fresh one-connection pool outside the shared pool: the run holds exactly one
    connection so the session-scoped advisory lock covers every per-file
    transaction, and a driver error here cannot evict the shared pool.
    """
    kwargs = settings.client_kwargs()
    kwargs["min_size"] = 1
    kwargs["max_size"] = 1
    async with (
        client_ctx(PostgresClient, fresh=True, **kwargs) as pool,
        pool.connection() as conn,
    ):
        yield conn


async def _read_history(conn: AsyncConnection, component: str) -> dict[int, tuple[str, str]]:
    try:
        cur = await conn.execute(
            "SELECT version, name, checksum FROM tai_schema_history WHERE component = %s",
            (component,),
        )
        rows = await cur.fetchall()
    except UndefinedTable:
        # A never-migrated database has no ``tai_schema_history`` table yet. That is
        # not an error to a reader: the component is simply fully pending. Creating
        # the table is the apply path's job alone (it owns the DDL identity and the
        # lock), so a read-only role that cannot CREATE reports cleanly instead of
        # crashing. The caller runs autocommit, so the failed SELECT poisons nothing.
        return {}
    return {int(version): (name, checksum) for version, name, checksum in rows}


async def _apply_component(conn: AsyncConnection, entry: MigrationEntry) -> list[AppliedMigration]:
    scripts = discover_migrations(entry.migrations_dir)
    applied = await _read_history(conn, entry.component)
    pending, mismatches = _reconcile(entry.component, scripts, applied)
    if mismatches:
        first = mismatches[0]
        detail = "no longer present" if first.current_checksum is None else f"now {first.current_checksum}"
        raise ChecksumMismatchError(
            f"component {entry.component!r} version {first.version} ({first.name!r}) was applied with checksum "
            f"{first.recorded_checksum} but is {detail}; the chain has been rewritten"
        )

    if applied and pending:
        # A pending version below the highest already-applied one is a back-inserted
        # migration: applying it now would run out of order (after later versions
        # already landed), so the applied schema would never match a from-scratch
        # replay. A schema-mutating runner refuses this loudly, like a tamper — only
        # the non-raising ``migration_status`` still reports it as plain pending data.
        max_applied = max(applied)
        out_of_order = [script for script in pending if script.version < max_applied]
        if out_of_order:
            versions = ", ".join(script.filename for script in out_of_order)
            raise MigrationError(
                f"component {entry.component!r} has pending migration(s) {versions} below the highest applied "
                f"version {max_applied}; a back-inserted / out-of-order migration cannot be applied after later "
                "ones — the chain must roll strictly forward"
            )

    outcomes: list[AppliedMigration] = []
    for script in pending:
        async with conn.transaction():
            # ``script.sql`` is packaged, trusted schema text (never user input), so
            # it is a valid literal query; the cast satisfies psycopg's LiteralString
            # guard, which exists to catch injected dynamic SQL.
            await conn.execute(cast(LiteralString, script.sql))
            await conn.execute(
                "INSERT INTO tai_schema_history (component, version, name, checksum) VALUES (%s, %s, %s, %s)",
                (entry.component, script.version, script.name, script.checksum),
            )
        outcomes.append(AppliedMigration(entry.component, script.version, script.name, script.checksum))
    return outcomes


def _group_by_dsn(entries: Sequence[MigrationEntry]) -> list[list[MigrationEntry]]:
    """Group entries by resolved DSN, preserving first-seen order. Each group
    shares one connection, one history table, and one lock."""
    groups: dict[str, list[MigrationEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.settings.pg_dsn, []).append(entry)
    return list(groups.values())


async def _acquire_lock(conn: AsyncConnection) -> None:
    # Blocking (not ``try``): a second runner waits for the first rather than
    # failing, so a rollout with two replicas resolves to one-applies/one-no-ops.
    await conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))


async def _release_lock(conn: AsyncConnection) -> None:
    try:
        await conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
    except Exception:
        # The polite release; closing the pinned connection is the real guarantee,
        # so a failure here (e.g. a connection already gone) is logged, not raised
        # over the run's own outcome.
        logger.warning("migrations: advisory unlock failed; connection close releases the lock", exc_info=True)


async def apply_migrations(entries: Sequence[MigrationEntry]) -> list[AppliedMigration]:
    """Apply every pending migration across ``entries``, returning one outcome
    per file applied.

    Entries are grouped by database; each group runs under its own session-scoped
    advisory lock on a single pinned connection, creating its ``tai_schema_history``
    table if absent. A checksum mismatch on any component raises
    :class:`ChecksumMismatchError` before that group applies anything further.
    """
    results: list[AppliedMigration] = []
    for group in _group_by_dsn(entries):
        async with _pinned_connection(group[0].settings) as conn:
            await conn.set_autocommit(True)
            await _acquire_lock(conn)
            try:
                await conn.execute(_HISTORY_DDL)
                for entry in group:
                    results.extend(await _apply_component(conn, entry))
            finally:
                await _release_lock(conn)
    return results


async def migration_status(entries: Sequence[MigrationEntry]) -> list[ComponentStatus]:
    """Report applied/pending/mismatch state per entry without applying or writing
    anything, one :class:`ComponentStatus` per input entry, in input order.

    Purely READ-ONLY: it issues NO DDL, so it runs under a lesser-privileged role
    that owns neither the history table nor CREATE on the schema (a boot gate reads
    through the feature's own store connection). A never-migrated database — no
    ``tai_schema_history`` table yet — reports every component fully pending rather
    than trying to create it. Never raises on a mismatch: the verdict is returned as
    data for ``tai db status`` and boot gates to render.

    Entries are grouped by resolved DSN for connection efficiency (one connection
    per database), but each entry gets its OWN status mapped back to its input
    position — two entries with the same component name but different DSNs are never
    collapsed, so a stale one can never be masked by an up-to-date sibling.
    """
    by_dsn: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        by_dsn.setdefault(entry.settings.pg_dsn, []).append(index)

    statuses: dict[int, ComponentStatus] = {}
    for indices in by_dsn.values():
        async with _pinned_connection(entries[indices[0]].settings) as conn:
            await conn.set_autocommit(True)
            for index in indices:
                entry = entries[index]
                scripts = discover_migrations(entry.migrations_dir)
                applied = await _read_history(conn, entry.component)
                pending, mismatches = _reconcile(entry.component, scripts, applied)
                statuses[index] = ComponentStatus(
                    component=entry.component,
                    applied_versions=tuple(sorted(applied)),
                    pending=pending,
                    mismatches=mismatches,
                )
    return [statuses[index] for index in range(len(entries))]


def _describe(statuses: Sequence[ComponentStatus]) -> str:
    """Render out-of-date component verdicts into one ``|``-joined refusal detail."""
    parts: list[str] = []
    for status in statuses:
        segments: list[str] = []
        if status.pending:
            pending = ", ".join(script.filename for script in status.pending)
            segments.append(f"{len(status.pending)} pending ({pending})")
        if status.mismatches:
            mismatched = ", ".join(str(mismatch.version) for mismatch in status.mismatches)
            segments.append(f"checksum mismatch on version(s) {mismatched}")
        parts.append(f"{status.component}: {'; '.join(segments)}")
    return " | ".join(parts)


async def assert_chain_applied(entries: Sequence[MigrationEntry], *, remediation: str) -> None:
    """Refuse to proceed unless every chain in ``entries`` is fully applied.

    The shared boot-gate primitive: reads state via :func:`migration_status` (which
    never raises on a mismatch — the verdict comes back as data) and, if any entry is
    not up to date, raises :class:`SchemaOutOfDateError` rendering the stale detail
    followed by the caller-supplied ``remediation`` sentence. Kit renders no CLI verb
    of its own — the CLI-facing caller owns the fix wording. A connection failure
    (unreachable / unconfigured Postgres) propagates as itself, so a broken gate is
    never mistaken for a clean one.
    """
    statuses = await migration_status(entries)
    stale = [status for status in statuses if not status.is_up_to_date]
    if not stale:
        return
    raise SchemaOutOfDateError(f"database schema is out of date — refusing to start: {_describe(stale)}. {remediation}")
