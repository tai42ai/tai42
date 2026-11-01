"""Core process settings and app-argument settings for the MCP server."""

from typing import Literal

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_contract.app import DEFAULT_LOCAL_PORT
from tai42_contract.sandbox import SandboxIsolation
from tai42_kit.settings import TaiBaseSettings


class CoreSettings(TaiBaseSettings):
    """Core process settings, read from the ``TAI_MCP_`` env prefix."""

    model_config = SettingsConfigDict(
        env_prefix="TAI_MCP_",
    )
    # TAI_MANIFEST_PATH is the single manifest-location env var: it feeds the
    # CLI's --manifest-path default (through the settings cache) and the config
    # file manager's manifest path (read per call). Deployment-spec identity (a
    # launcher-set bootstrap path) — a profile can never carry it, so it is
    # excluded from the reload boundary.
    manifest_path: str | None = Field(
        default=None,
        validation_alias="TAI_MANIFEST_PATH",
        description=(
            "The manifest file the server loads. Unset, the file config mode reads "
            "<config dir>/manifest.yml; `tai serve` and `tai setup --recover` require --manifest-path; "
            "`tai config lint` skips the manifest check unless a file is given."
        ),
        json_schema_extra={"reload": "excluded"},
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
    # The start budget of an MCP server viability check (connect + list_tools): the
    # seconds a server start may take before it is torn down, on every door — boot,
    # reload, the re-probe task, the reload doors and the connector liveness probe.
    # A start that exceeds it is torn down instead of blocking the whole server.
    # Boot, a reload, the re-probe task and the reload doors then record the server
    # unavailable, a start they stopped waiting for through the ``starting`` record
    # that waits on it; the connector liveness probe reads the server not live and
    # logs the start's late outcome.
    mcp_probe_timeout: float = 15.0
    # How long a RELOAD (an epoch rebuild) WAITS for a server start, kept SHORT and
    # separate from the start budget: a reload runs while the fleet is live and holds
    # the reload gate, so waiting the whole start budget for a slow or unreachable
    # server would stall every reload-gated write and fleet-reload convergence. It
    # bounds only the wait and never tears a start down: a start still in progress
    # when it ends is recorded ``starting``, goes on up to ``mcp_probe_timeout`` and
    # binds once its start lists its tools — degraded-but-live, never a stalled
    # fleet. With no serving loop running to apply a late outcome on, the whole
    # start budget is waited instead.
    mcp_reload_probe_timeout: float = Field(default=3.0, gt=0)
    # The failed-MCP re-probe backoff bounds. The lifespan-owned re-probe task
    # sleeps ``initial`` seconds between passes, doubling up to ``max`` after a
    # pass where every probed server stayed down, and resetting to ``initial``
    # whenever a server recovers or a new one fails.
    mcp_reprobe_initial_seconds: float = Field(default=30.0, gt=0)
    mcp_reprobe_max_seconds: float = Field(default=600.0, gt=0)


class AppArgsSettings(TaiBaseSettings):
    """App-argument settings, read from the ``APP_ARGS_`` env prefix."""

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
    # indefinitely, so the lifespan teardown always runs. A served worker ends its
    # event streams itself once its in-flight replies are written
    # (``app/serve_exit.py``); this window is the cap on that wait too. A shipped
    # ``--timeout-graceful-shutdown`` CLI extra-arg overrides this default. Must
    # be positive. The ``tai serve`` launcher reads it once to build the uvicorn
    # config it hands to every worker it spawns or respawns, so only a launcher
    # restart changes it — excluded, like the bind fields.
    timeout_graceful_shutdown: int = Field(default=10, gt=0, json_schema_extra={"reload": "excluded"})
