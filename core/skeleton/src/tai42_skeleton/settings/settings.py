from typing import Literal

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_cli.context import DEFAULT_LOCAL_PORT
from tai42_contract.sandbox import SandboxIsolation
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
    # The scalar sandbox-provider slot loads via the manifest ``sandbox_module``
    # exactly as the backend does; this env selects nothing on its own and only
    # names the slot in the ``require_sandbox`` unavailable message. Binds pooled
    # runtime built at boot, so a change converges through a process recycle.
    sandbox: str | None = Field(default=None, json_schema_extra={"reload": "recycle"})
    # Security-as-config: the four PLATFORM-owned sandbox policy knobs the skeleton
    # resolves into one ``SandboxPolicy`` and binds to the kit session-create
    # chokepoint at provider registration. ALL FOUR are recycle-class: the policy is
    # bound ONCE at registration and never re-bound on a hot apply, so a hot change
    # would leave the kit enforcing the boot snapshot while the identity door reports
    # the new value — recycle re-imports the scalar ``sandbox_module`` and re-binds
    # the freshly-resolved policy, keeping enforcement and the door consistent.
    # ``sandbox_egress`` is the network CEILING (default OPEN); ``sandbox_isolation``
    # is the strength FLOOR; ``sandbox_scrub_transcript`` is carried for the consumer
    # to apply (off by default); ``sandbox_durable`` gates whether a persistent
    # session is permitted at all.
    sandbox_egress: Literal["none", "internal", "egress"] = Field(
        default="egress", json_schema_extra={"reload": "recycle"}
    )
    sandbox_isolation: SandboxIsolation = Field(default="container", json_schema_extra={"reload": "recycle"})
    sandbox_scrub_transcript: bool = Field(default=False, json_schema_extra={"reload": "recycle"})
    sandbox_durable: bool = Field(default=True, json_schema_extra={"reload": "recycle"})
    # Max seconds to spend on a single MCP server viability check (connect +
    # list_tools) at COLD BOOT. A server that exceeds this is skipped and
    # recorded instead of blocking the whole server.
    mcp_probe_timeout: float = 15.0
    # The same viability check budget during a RELOAD (an epoch rebuild), kept
    # SHORT and separate from the generous cold-boot value: a reload runs while
    # the fleet is live and holds the reload gate, so an unreachable MCP server
    # blocking the probe for the full boot budget would stall every reload-gated
    # write and fleet-reload convergence. A server that overruns this shorter
    # budget is recorded unavailable (its tools bind a moment later via the
    # lifespan re-probe task / the ``reload_failed_mcps`` door) rather than
    # gating the whole reload — degraded-but-live, never a stalled fleet.
    mcp_reload_probe_timeout: float = Field(default=3.0, gt=0)
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
    # The CLI's default ``--server`` URL binds the same constant, so a default
    # ``tai serve`` and the default remote target cannot drift.
    port: int = Field(default=DEFAULT_LOCAL_PORT, json_schema_extra={"reload": "excluded"})
    uds: str | None = Field(default=None, json_schema_extra={"reload": "excluded"})

    # uvicorn's graceful-shutdown budget (seconds): on SIGTERM uvicorn force-
    # completes in-flight requests within this window instead of waiting
    # indefinitely, so the lifespan teardown always runs. A shipped
    # ``--timeout-graceful-shutdown`` CLI extra-arg overrides this default. Must
    # be positive. Boot-read, so it converges via respawn (env is bootstrapped
    # before the read) — recycle, not excluded.
    timeout_graceful_shutdown: int = Field(default=10, gt=0, json_schema_extra={"reload": "recycle"})
