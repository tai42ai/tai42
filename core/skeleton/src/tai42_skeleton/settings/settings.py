from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings


class CoreSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TAI_MCP_",
    )
    # TAI_MANIFEST_PATH is the single manifest-location env var: it feeds the
    # CLI's --manifest-path default here and is what the config file manager
    # reads directly. Deployment-spec identity (a launcher-set bootstrap path) —
    # a profile can never carry it, so it is excluded from the reload boundary.
    manifest_path: str | None = Field(
        default=None, validation_alias="TAI_MANIFEST_PATH", json_schema_extra={"reload": "excluded"}
    )
    # Backend/template selection binds pooled resources built at boot; a change
    # converges through a process recycle, not an in-process flip.
    backend: str | None = Field(default=None, json_schema_extra={"reload": "recycle"})
    template: str | None = Field(default=None, json_schema_extra={"reload": "recycle"})
    # Max seconds to spend on a single MCP server viability check (connect +
    # list_tools) during startup or reload. A server that exceeds this is
    # skipped and recorded instead of blocking the whole server.
    mcp_probe_timeout: float = 15.0
    # The failed-MCP re-probe backoff bounds. The lifespan-owned re-probe task
    # sleeps ``initial`` seconds between passes, doubling up to ``max`` after a
    # pass where every probed server stayed down, and resetting to ``initial``
    # whenever a server recovers or a new one fails.
    mcp_reprobe_initial_seconds: float = Field(default=30.0, gt=0)
    mcp_reprobe_max_seconds: float = Field(default=600.0, gt=0)


class AppArgsSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_ARGS_",
    )

    # Serve bind is deployment-spec identity: the launcher owns it and a
    # profile never carries it, so each bind field is excluded from the reload
    # boundary. The metrics exporter and the serve socket are pinned the same way.
    transport: str = Field(default="http", json_schema_extra={"reload": "excluded"})
    host: str = Field(default="127.0.0.1", json_schema_extra={"reload": "excluded"})
    port: int = Field(default=8000, json_schema_extra={"reload": "excluded"})
    uds: str | None = Field(default=None, json_schema_extra={"reload": "excluded"})

    # uvicorn's graceful-shutdown budget (seconds): on SIGTERM uvicorn force-
    # completes in-flight requests within this window instead of waiting
    # indefinitely, so the lifespan teardown always runs. A shipped
    # ``--timeout-graceful-shutdown`` CLI extra-arg overrides this default. Must
    # be positive. Boot-read, so it converges via respawn (env is bootstrapped
    # before the read) — recycle, not excluded.
    timeout_graceful_shutdown: int = Field(default=10, gt=0, json_schema_extra={"reload": "recycle"})
