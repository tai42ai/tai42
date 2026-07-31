"""Client connection settings.

``ClientSettings`` is the base a client's settings subclass extends, returning the
kwargs its ``PooledClient._create`` needs via ``client_kwargs()`` — the
JSON-serializable connection identity the facade keys its per-loop pool on.
A feature subclasses one of the concrete settings with its own ``env_prefix``
(and any feature fields), then gets a client via ``client_ctx(RedisClient, settings)``.
"""

import math
from typing import Any, ClassVar
from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import TaiBaseSettings, settings_cache


class ClientSettings(TaiBaseSettings):
    # Abstract base — claims no env vars of its own; excluded from the settings
    # registry. Own-attribute flag, so concrete product subclasses still register.
    registry_exclude: ClassVar[bool] = True

    def client_kwargs(self) -> dict[str, Any]:
        """Connection kwargs passed to the client's ``_create`` / ``current``."""
        raise NotImplementedError


class RedisConnectionSettings(ClientSettings):
    # Base with unprefixed field names — a product subclasses it with its own
    # ``env_prefix``; excluded here so the unprefixed base is not a bogus group.
    registry_exclude: ClassVar[bool] = True

    redis_url: str | None = None
    redis_max_connections: int | None = None
    decode_responses: bool = True

    # Resilience — opt-in. Defaults match redis-py's no-timeout/no-retry behavior,
    # so a feature that sets none of these connects exactly as a bare from_url.
    socket_timeout: float | None = None
    # Connect-phase read/socket timeout — unset means redis-py's own default; a
    # set value must be positive. Bounds the initial connect, unlike socket_timeout
    # which also applies to blocking commands.
    socket_connect_timeout: float | None = Field(default=None, gt=0)
    retry_on_timeout: bool = False
    retry_attempts: int = 0  # >0 attaches an exponential-backoff Retry on ConnectionError/TimeoutError

    def client_kwargs(self) -> dict[str, Any]:
        """Connection identity for the pooled client — JSON-serializable only.

        The facade keys its per-loop connection pool on these kwargs, so every
        value must be JSON-serializable. ``retry_attempts`` travels as an int;
        the redis client turns it into a ``Retry`` object at construction time —
        a ``Retry`` instance is not serializable and would break the pool key.
        """
        kwargs: dict[str, Any] = {
            "url": self.redis_url,
            "max_connections": self.redis_max_connections,
            "decode_responses": self.decode_responses,
        }
        if self.socket_timeout is not None:
            kwargs["socket_timeout"] = self.socket_timeout
        if self.socket_connect_timeout is not None:
            kwargs["socket_connect_timeout"] = self.socket_connect_timeout
        if self.retry_on_timeout:
            kwargs["retry_on_timeout"] = True
        if self.retry_attempts > 0:
            kwargs["retry_attempts"] = self.retry_attempts
        return kwargs


class PostgresConnectionSettings(ClientSettings):
    # Base with unprefixed field names — a product subclasses it with its own
    # ``env_prefix``; excluded here so the unprefixed base is not a bogus group.
    registry_exclude: ClassVar[bool] = True

    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_db: str = "postgres"
    pg_user: str = "postgres"
    # No baked-in credential — supply via env (PG_PASSWORD). Empty by default so a
    # missing password fails at connect time rather than shipping a real secret.
    # SecretStr keeps it out of repr/logs/tracebacks; the plaintext is read only
    # when composing the DSN handed to the driver.
    pg_password: SecretStr = SecretStr("")
    pg_min_connections: int = 2
    pg_max_connections: int = 10
    # libpq connect_timeout (seconds) for each new pool connection. Must be positive.
    pg_connect_timeout: int = Field(default=10, gt=0)
    # Server-side statement_timeout applied to every session of this pool. Bounds a
    # mid-statement stall; a legitimately longer statement needs the operator to
    # raise this. Must be positive.
    pg_statement_timeout_seconds: float = Field(default=60, gt=0)

    @property
    def pg_dsn(self) -> str:
        # Percent-encode every user-controlled component so reserved characters
        # can't break the URL or reroute the host:
        #  - credentials (@ / : # ?) in a generated password/user,
        #  - the db name, which is a single path segment (a '/' or '?' in it
        #    would otherwise start a new path/query),
        # and bracket an IPv6 host so its colons aren't read as the port
        # separator (``[::1]:5432`` rather than ``::1:5432``).
        user = quote(self.pg_user, safe="")
        password = quote(self.pg_password.get_secret_value(), safe="")
        db = quote(self.pg_db, safe="")
        host = self.pg_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        # Two libpq conninfo params carried in the DSN's query string: the
        # connect-phase timeout and a session ``statement_timeout`` (whole ms).
        # ``options`` is percent-encoded with the same ``quote(safe="")``
        # discipline as the credentials so its space and ``=`` survive as
        # ``%20`` / ``%3D`` rather than breaking the query.
        # Round UP to whole ms: a positive sub-millisecond value must never floor to
        # ``statement_timeout=0``, which Postgres reads as DISABLED (unbounded) — the
        # opposite of a configured bound. ``ceil`` keeps any value > 0 at >= 1 ms and
        # is exact for whole-ms values (60 -> 60000, 1.5 -> 1500).
        statement_timeout_ms = math.ceil(self.pg_statement_timeout_seconds * 1000)
        options = quote(f"-c statement_timeout={statement_timeout_ms}", safe="")
        query = f"connect_timeout={self.pg_connect_timeout}&options={options}"
        return f"postgresql://{user}:{password}@{host}:{self.pg_port}/{db}?{query}"

    def client_kwargs(self) -> dict[str, Any]:
        """Connection identity for the pooled client — JSON-serializable only.

        Pools are keyed by DSN + min/max size; callers that disagree on pool
        bounds get separate pools.
        """
        return {
            "dsn": self.pg_dsn,
            "min_size": self.pg_min_connections,
            "max_size": self.pg_max_connections,
        }


class MCPClientSettings(TaiBaseSettings):
    """Timeouts governing MCP client connect/init and per-tool-call dispatch.

    These are behavior, not connection identity, so they are deliberately NOT
    part of any pooled-client key — tuning a timeout must not split the pool.
    """

    model_config = SettingsConfigDict(env_prefix="MCP_CLIENT_")

    # Budget for establishing + initializing one downstream MCP client
    # connection. Held inside the pooled creation lock, so it also bounds how
    # long a connect-stall can queue same-config callers. Must be positive.
    connect_timeout_seconds: float = Field(default=30, gt=0)
    # Wall-clock budget for one downstream MCP tool call made through a
    # kit-bound LangChain tool. Must be positive.
    call_timeout_seconds: float = Field(default=300, gt=0)


@settings_cache
def mcp_client_settings() -> MCPClientSettings:
    """Return the process-wide :class:`MCPClientSettings`, cached after first load."""
    return MCPClientSettings()
