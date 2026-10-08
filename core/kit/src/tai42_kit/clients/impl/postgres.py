"""Pooled Postgres client built on ``psycopg_pool.AsyncConnectionPool``."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Final

from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import TupleRow
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from tai42_kit.clients.base import PooledClient, reject_unknown_connection_kwargs
from tai42_kit.clients.impl.postgres_json import configure_platform_json
from tai42_kit.clients.settings import PostgresConnectionSettings

# ``Json`` is re-exported so callers wrap jsonb params through the pooled
# client's own module rather than importing psycopg directly — every pg
# primitive the app touches is reached through the kit client layer.
__all__ = [
    "EXPLICIT_DSN_POOL_MAX_SIZE",
    "EXPLICIT_DSN_POOL_MIN_SIZE",
    "Json",
    "PostgresClient",
    "open_dedicated_pool",
    "pinned_connection",
    "postgres_pool_name",
    "read_connection",
]

# A callback psycopg_pool runs with one connection (``configure=`` on every new
# connection, ``reset=`` on every connection returned to the pool).
ConnectionCallback = Callable[[AsyncConnection[Any]], Awaitable[None]]

# The pool's default connection type. Naming it lets the pooled-client generic
# resolve concretely (the AsyncConnectionPool default hides ACT behind a cast).
_Pool = AsyncConnectionPool[AsyncConnection[TupleRow]]

# Connection kwargs a Postgres client accepts. ``dsn`` is the pool identity;
# ``min_size``/``max_size`` are this database's deployment build options; and
# ``env_prefix`` rides along (a message-only passenger) so the pool name can carry
# the owning component. Anything else is rejected so it can't silently change the
# build.
_ALLOWED_KWARGS = frozenset({"dsn", "min_size", "max_size", "env_prefix"})

# Default pool bounds when a caller supplies neither — the pooled-client defaults
# for a plain ``PostgresClient`` built from raw kwargs.
_DEFAULT_MIN_SIZE = 2
_DEFAULT_MAX_SIZE = 10

# The ``TAI_DATABASE_<NAME>_`` prefix a database's settings carry; the owning
# component in a pool name is what remains after stripping it.
_DATABASE_ENV_PREFIX = "TAI_DATABASE_"

# The pool owner when no settings namespace names one.
_DEFAULT_POOL_OWNER = "postgres"

# Fallback budget for filling ``min_size`` connections when the DSN sets no
# ``connect_timeout`` of its own.
_DEFAULT_OPEN_TIMEOUT = 30.0


def _open_timeout(dsn: str, min_size: int) -> float:
    """Seconds allowed for the initial ``min_size`` fill.

    Derived from the DSN's own ``connect_timeout`` so a deployment tunes ONE
    knob rather than two, budgeting that per connection in the fill. A DSN that
    sets none, sets something unparseable, or sets a non-positive value (libpq
    reads 0 as "no timeout", but the fill budget must stay finite) falls back
    to the fixed default.
    """
    try:
        raw = conninfo_to_dict(dsn).get("connect_timeout")
    except Exception:  # a malformed DSN fails loudly at connect, not here
        raw = None
    if raw in (None, ""):
        return _DEFAULT_OPEN_TIMEOUT
    try:
        per_connection = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_OPEN_TIMEOUT
    if per_connection <= 0:
        return _DEFAULT_OPEN_TIMEOUT
    return per_connection * max(min_size, 1)


def _pool_owner(env_prefix: str | None) -> str:
    """The owning component of a pool name, derived from a settings env prefix.

    Strips a leading ``TAI_DATABASE_`` (case-insensitively — the registry uppercases
    prefixes, but a lower- or mixed-case one must derive the same owner rather than
    leak the raw prefix) and the trailing ``_``, lower-cases the rest; ``postgres``
    when the prefix is absent or empty. A database declared under
    ``TAI_DATABASE_SKELETON_`` therefore owns pools named ``skeleton@…``.
    """
    prefix = (env_prefix or "").strip()
    if prefix.upper().startswith(_DATABASE_ENV_PREFIX):
        prefix = prefix[len(_DATABASE_ENV_PREFIX) :]
    owner = prefix.rstrip("_").lower()
    return owner or _DEFAULT_POOL_OWNER


def postgres_pool_name(dsn: str, owner: str, role: str = "") -> str:
    """The psycopg_pool ``name`` for a database pool: ``<owner>@<host>/<db>``.

    Host and database come from the DSN via ``conninfo_to_dict`` — never the user
    or password, which must not reach a pool name that surfaces in log lines. A
    non-empty ``role`` appends a ``:<role>`` suffix so a dedicated pool (a pinned
    connection, a langgraph store/checkpoint pool) is distinguishable in the logs.
    """
    info = conninfo_to_dict(dsn)
    host = info.get("host") or ""
    dbname = info.get("dbname") or ""
    name = f"{owner}@{host}/{dbname}"
    return f"{name}:{role}" if role else name


def _mask_dsn_password(dsn: str) -> str:
    """The DSN with any password replaced by ``***`` for a safe error message.

    A DSN the parser cannot read carries a password this helper cannot locate to
    mask, so it is never echoed back: a fully redacted placeholder is returned
    rather than risk emitting a credential in an error message.
    """
    try:
        info = conninfo_to_dict(dsn)
    except Exception:
        return "<unparseable DSN>"
    if info.get("password"):
        info["password"] = "***"  # noqa: S105 -- a mask over the real secret, not a credential
    return make_conninfo("", **info)


async def _open_named_pool(
    dsn: str,
    *,
    min_size: int,
    max_size: int,
    name: str,
    connection_kwargs: dict | None = None,
    configure: ConnectionCallback | None = None,
    reset: ConnectionCallback | None = None,
) -> _Pool:
    """Prove the first connection, then build and fill a named pool.

    Prove the first connection with the driver itself BEFORE the pool is built and
    handed out. An ``AsyncConnectionPool`` opened non-blocking fills from background
    workers that only log and reschedule a refused / unreachable / unauthorized /
    TLS-timeout connect, so a caller would block the pool's checkout timeout and then
    see a pool error, never the real driver failure. One bounded ``connect`` — bounded
    by the DSN's own ``connect_timeout``, which libpq applies across the whole
    establishment including the TLS handshake — surfaces that failure here as the real
    ``psycopg.OperationalError``, propagated unwrapped to the caller with no pool built
    and no worker left retrying. A ``min_size > 1`` fill then proceeds in the
    background: the server is proven reachable, and a later fill failure is what the
    pool's own retry and checkout-time ``check`` exist for.

    ``connection_kwargs`` (psycopg_pool's ``kwargs=``) are applied to every connection
    the pool opens — a caller whose driver needs autocommit, a row factory, or prepared
    statements disabled passes them here. ``configure`` runs on every new connection
    (the kit's own pools register their json loader there); ``reset`` runs on every
    connection returned to the pool once it is idle, and a connection it fails on or
    leaves outside a transaction-idle state is discarded.
    """
    async with await AsyncConnection.connect(dsn):
        pass

    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        open=False,
        name=name,
        connection_class=AsyncConnection,
        kwargs=connection_kwargs,
        configure=configure,
        reset=reset,
        # Validate each connection on checkout: a connection the server has
        # since closed (idle drop, restart, a severed-then-restored link) is
        # discarded and replaced instead of handed to the caller, who would
        # otherwise get an OperationalError on the first query. This is what
        # lets a pooled caller ride out a transient Postgres outage without a
        # restart.
        check=AsyncConnectionPool.check_connection,
    )
    # ``wait=True`` so the initial fill finishes HERE. Opened non-blocking, the
    # fill runs in background workers that outlive the caller: at loop shutdown
    # they are cancelled mid-connect, psycopg_pool logs the cancellation as
    # ``error connecting in '<name>': `` (an empty cause, since ``CancelledError``
    # has no message), and process exit wedges — a one-shot command such as
    # ``tai db migrate`` applies its migrations and then never returns. The probe
    # above still owns first-connection diagnostics: this wait raises
    # ``PoolTimeout``, never the underlying driver error.
    try:
        await pool.open(wait=True, timeout=_open_timeout(dsn, min_size))
    except BaseException:
        # BaseException, not Exception: a cancelled ``open`` would otherwise
        # leak the very pool whose lifetime this guard exists to bound.
        await pool.close()
        raise
    return pool


async def _restore_transactional(conn: AsyncConnection[Any]) -> None:
    """Return a connection :func:`read_connection` switched to autocommit to the pool's mode."""
    if conn.autocommit:
        await conn.set_autocommit(False)


@asynccontextmanager
async def read_connection(pool: AsyncConnectionPool[Any]) -> AsyncIterator[AsyncConnection[Any]]:
    """A pooled connection in autocommit mode for stand-alone reads: each statement runs on its own, no BEGIN/COMMIT.

    Plain SELECTs only. A locking read (FOR UPDATE / FOR SHARE), a write, or statements
    that must be atomic together run on ``pool.connection()`` inside
    ``conn.transaction()``. The pool must be a :class:`PostgresClient` pool, whose reset
    callback returns the connection to transactional mode when it comes back.
    """
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        yield conn


# The kit's sizes for a dedicated pool on an explicit DSN: a DSN string names a database other
# than a settings namespace, so no deployment sizes apply to it.
EXPLICIT_DSN_POOL_MIN_SIZE: Final = 1
EXPLICIT_DSN_POOL_MAX_SIZE: Final = 20


async def open_dedicated_pool(
    target: PostgresConnectionSettings | str, *, role: str, connection_kwargs: dict[str, Any] | None = None
) -> _Pool:
    """Open a dedicated (not shared) named pool for ``role``, its first connection proven and its fill awaited.

    A settings object supplies the DSN and the deployment's pool sizes; a DSN string gets
    :data:`EXPLICIT_DSN_POOL_MIN_SIZE` / :data:`EXPLICIT_DSN_POOL_MAX_SIZE`. The pool is named
    ``postgres@<host>/<db>:<role>``; ``connection_kwargs`` apply to every connection it opens. The
    caller owns the pool and closes it.
    """
    if isinstance(target, str):
        dsn, min_size, max_size = target, EXPLICIT_DSN_POOL_MIN_SIZE, EXPLICIT_DSN_POOL_MAX_SIZE
    else:
        dsn, min_size, max_size = target.pg_dsn, target.pg_min_connections, target.pg_max_connections
    return await _open_named_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        name=postgres_pool_name(dsn, _pool_owner(None), role),
        connection_kwargs=connection_kwargs,
    )


class PostgresClient(PooledClient[_Pool]):
    """Pooled ``psycopg_pool.AsyncConnectionPool``, one pool per DSN per loop.

    The pool is named ``<owner>@<host>/<db>``; ``min_size``/``max_size`` are this
    database's deployment settings read when the pool is built; a second settings
    prefix resolving to the same DSN with different sizes raises. Compose
    ``PostgresConnectionSettings`` for the connection kwargs.
    """

    def _identity(self, **kwargs) -> dict:
        return {"dsn": kwargs["dsn"]}

    def _default_build_options(self) -> dict:
        # The pool bounds a caller who omits them ends up with — the same defaults
        # ``_create`` applies. Comparing the effective build lets a caller that omits
        # the sizes and one that passes these exact values share one pool.
        return {"min_size": _DEFAULT_MIN_SIZE, "max_size": _DEFAULT_MAX_SIZE}

    def _describe_identity(self, **kwargs) -> str:
        return _mask_dsn_password(kwargs.get("dsn", ""))

    async def _create(self, **kwargs) -> _Pool:
        if "dsn" not in kwargs:
            raise KeyError("Postgres client requires a 'dsn' kwarg")
        dsn = kwargs["dsn"]
        if not dsn or not isinstance(dsn, str):
            raise ValueError("Postgres client 'dsn' must be a non-empty string")
        reject_unknown_connection_kwargs("Postgres client", kwargs, _ALLOWED_KWARGS)

        name = postgres_pool_name(dsn, _pool_owner(kwargs.get("env_prefix")))
        return await _open_named_pool(
            dsn,
            min_size=kwargs.get("min_size", _DEFAULT_MIN_SIZE),
            max_size=kwargs.get("max_size", _DEFAULT_MAX_SIZE),
            name=name,
            configure=configure_platform_json,
            reset=_restore_transactional,
        )

    async def _close(self, client: _Pool):
        await client.close()

    def _disconnection_exceptions(self) -> tuple[type[Exception], ...]:
        import psycopg

        return (psycopg.OperationalError,)


@asynccontextmanager
async def pinned_connection(settings: PostgresConnectionSettings) -> AsyncIterator[AsyncConnection]:
    """Yield one dedicated connection held for the whole context.

    A one-connection pool (``min_size == max_size == 1``) built outside the shared
    cache, named ``<owner>@<host>/<db>:pinned``, opened through the same probe and
    bounded fill as the pooled path. The caller holds exactly one connection, so a
    session-scoped advisory lock taken on it covers every statement in the body, and
    a driver error on it never touches the shared pool. The pool and its connection
    close on any exit — normal return, exception, or cancellation.
    """
    kwargs = settings.client_kwargs()
    dsn = kwargs["dsn"]
    name = postgres_pool_name(dsn, _pool_owner(kwargs.get("env_prefix")), "pinned")
    pool = await _open_named_pool(dsn, min_size=1, max_size=1, name=name, configure=configure_platform_json)
    try:
        async with pool.connection() as conn:
            yield conn
    finally:
        await pool.close()
