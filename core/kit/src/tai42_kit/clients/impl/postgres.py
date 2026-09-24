"""Pooled Postgres client built on ``psycopg_pool.AsyncConnectionPool``."""

from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import TupleRow
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from tai42_kit.clients.base import PooledClient, reject_unknown_connection_kwargs

# ``Json`` is re-exported so callers wrap jsonb params through the pooled
# client's own module rather than importing psycopg directly — every pg
# primitive the app touches is reached through the kit client layer.
__all__ = ["Json", "PostgresClient"]

# The pool's default connection type. Naming it lets the pooled-client generic
# resolve concretely (the AsyncConnectionPool default hides ACT behind a cast).
_Pool = AsyncConnectionPool[AsyncConnection[TupleRow]]

# Connection kwargs a Postgres client accepts — the identity produced by
# ``PostgresConnectionSettings.client_kwargs``. Anything else is rejected so it
# can't silently split the pool key.
_ALLOWED_KWARGS = frozenset({"dsn", "min_size", "max_size"})

# Fallback budget for filling ``min_size`` connections when the DSN sets no
# ``connect_timeout`` of its own.
_DEFAULT_OPEN_TIMEOUT = 30.0


def _open_timeout(dsn: str, min_size: int) -> float:
    """Seconds allowed for the initial ``min_size`` fill.

    Derived from the DSN's own ``connect_timeout`` so a deployment tunes ONE
    knob rather than two, budgeting that per connection in the fill. A DSN that
    sets none, or sets something unparseable, falls back to the fixed default.
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
    return per_connection * max(min_size, 1)


class PostgresClient(PooledClient[_Pool]):
    """Pooled ``psycopg_pool.AsyncConnectionPool``, one pool per (DSN, size) per loop.

    Compose ``PostgresConnectionSettings`` for the connection kwargs.
    """

    async def _create(self, **kwargs) -> _Pool:
        if "dsn" not in kwargs:
            raise KeyError("Postgres client requires a 'dsn' kwarg")
        dsn = kwargs["dsn"]
        if not dsn or not isinstance(dsn, str):
            raise ValueError("Postgres client 'dsn' must be a non-empty string")
        reject_unknown_connection_kwargs("Postgres client", kwargs, _ALLOWED_KWARGS)

        # Prove the first connection with the driver itself BEFORE the pool is built and
        # handed out. An ``AsyncConnectionPool`` opened non-blocking fills from background
        # workers that only log and reschedule a refused / unreachable / unauthorized /
        # TLS-timeout connect, so a caller would block the pool's checkout timeout and then
        # see a pool error, never the real driver failure. One bounded ``connect`` — bounded
        # by the DSN's own ``connect_timeout``, which libpq applies across the whole
        # establishment including the TLS handshake — surfaces that failure here as the real
        # ``psycopg.OperationalError``, propagated unwrapped to the ``client_ctx`` caller with
        # no pool built and no worker left retrying. A ``min_size > 1`` fill then proceeds in
        # the background: the server is proven reachable, and a later fill failure is what the
        # pool's own retry and checkout-time ``check`` exist for.
        async with await AsyncConnection.connect(dsn):
            pass

        min_size = kwargs.get("min_size", 2)
        pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=kwargs.get("max_size", 10),
            open=False,
            connection_class=AsyncConnection,
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
        # ``error connecting in 'pool-N': `` (an empty cause, since
        # ``CancelledError`` has no message), and process exit wedges — a one-shot
        # command such as ``tai db migrate`` applies its migrations and then never
        # returns. The probe above still owns first-connection diagnostics: this
        # wait raises ``PoolTimeout``, never the underlying driver error.
        try:
            await pool.open(wait=True, timeout=_open_timeout(dsn, min_size))
        except BaseException:
            # BaseException, not Exception: a cancelled ``open`` would otherwise
            # leak the very pool whose lifetime this guard exists to bound.
            await pool.close()
            raise
        return pool

    async def _close(self, client: _Pool):
        await client.close()

    def _disconnection_exceptions(self) -> tuple[type[Exception], ...]:
        import psycopg

        return (psycopg.OperationalError,)
