"""Read-only status of every pluggable kind — the introspection seam.

:func:`collect_kind_status` reports one :class:`KindStatus` row per pluggable
kind (identity, accounts, monitoring, storage, backend, channels, webhook
verifiers, config, studio plugins), reading the live registries WITHOUT mutating
any of them. Each row's ``state`` is ``active`` (a real plugin is registered),
``default`` (a built-in fallback is serving — the NoOp monitoring recorder or the
``file`` config provider), or ``off`` (nothing registered and the kind has no
built-in fallback — a legal, reported state, never an error).

The startup summary (:mod:`tai42_skeleton.app.lifecycle`) and the
``GET /api/system/kinds`` route both render these rows, so the module lives beside
the app rather than in a router: the startup summary reports in every deployment
regardless of which routers a manifest mounts. App-bound facets are read through
the bound ``tai42_app`` handle so the summary reports the app being started and the
route reports the running app. Every read that can legally find a kind absent
returns an ``off``/``default`` row; any other error propagates so a broken
registry surfaces loudly instead of as a silent partial table.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel
from tai42_contract.access_control.registry import get_identity_provider_factory
from tai42_contract.accounts.registry import iter_accounts_provider_factories
from tai42_contract.app import tai42_app

from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.config.config_mode import config_mode
from tai42_skeleton.connectors.settings import connectors_store_configured
from tai42_skeleton.interactions.settings import interactions_store_configured
from tai42_skeleton.marketplace.settings import marketplace_store_configured
from tai42_skeleton.monitoring.noop import NoOpMonitoring
from tai42_skeleton.monitoring.registry import get_monitoring
from tai42_skeleton.plugins.registry import StudioPluginError, current_registry
from tai42_skeleton.routers.tool_runs_settings import tool_runs_store_configured
from tai42_skeleton.settings.rate_limit import RateLimitSettings
from tai42_skeleton.tool_meta.settings import tool_meta_store_configured
from tai42_skeleton.versioning import versioned_store_configured

if TYPE_CHECKING:
    from tai42_skeleton.app.facets import AdminFacet, StorageFacet

# Once-per-process guard for the NoOp-monitoring warning: the startup summary
# fires it at most once even across in-place reloads (each reload re-runs the
# summary), so a monitoring-less deployment logs the warning a single time.
_NOOP_WARNED = False

# The operator-facing warning emitted once when NoOp monitoring is the active
# backend at startup-summary time — the point where "monitoring not configured"
# becomes distinguishable from "no traffic".
_NOOP_MONITORING_WARNING = (
    "monitoring: OFF — no recorder plugin installed; runs are not recorded and "
    "observability dashboards will show zeros. Install a monitoring plugin (manifest "
    "monitoring_module) to enable recording."
)

State = Literal["active", "default", "off"]


class KindStatus(BaseModel):
    """One pluggable kind's live status. ``plugin`` is the serving module/provider
    name when a real implementation is registered, else ``None``; ``detail`` is a
    short human string (provider names, channel names, or the absence reason)."""

    kind: str
    state: State
    plugin: str | None
    detail: str


def _identity_provider_registered(name: str) -> bool:
    """Whether an identity provider is registered under ``name`` — the registry's
    ``KeyError``-on-miss lookup reported as a boolean."""
    try:
        get_identity_provider_factory(name)
        return True
    except KeyError:
        return False


def _identity_row() -> KindStatus:
    settings = access_control_settings()
    if not settings.enable:
        return KindStatus(kind="identity", state="off", plugin=None, detail="access control disabled")
    names = settings.auth_providers
    parts = [name if _identity_provider_registered(name) else f"{name} (not registered)" for name in names]
    return KindStatus(
        kind="identity",
        state="active",
        plugin=", ".join(names),
        detail="providers: " + ", ".join(parts),
    )


def _accounts_row() -> KindStatus:
    names = [name for name, _factory in iter_accounts_provider_factories()]
    if not names:
        return KindStatus(kind="accounts", state="off", plugin=None, detail="no accounts provider registered")
    return KindStatus(
        kind="accounts",
        state="active",
        plugin=", ".join(names),
        detail="providers: " + ", ".join(names),
    )


def _monitoring_row() -> KindStatus:
    backend = get_monitoring()
    if isinstance(backend, NoOpMonitoring):
        return KindStatus(
            kind="monitoring",
            state="default",
            plugin=None,
            detail="NoOpMonitoring — no recorder plugin installed",
        )
    cls = type(backend)
    return KindStatus(kind="monitoring", state="active", plugin=cls.__module__, detail=cls.__qualname__)


def _storage_row() -> KindStatus:
    # ``provider`` rides the skeleton ``StorageFacet``, not the tai42-contract
    # ``AppStorage`` protocol, so the bound handle is read through the concrete facet.
    provider = cast("StorageFacet", tai42_app.storage).provider
    if provider is None:
        return KindStatus(
            kind="storage",
            state="off",
            plugin=None,
            detail="dead by default — no storage provider installed",
        )
    cls = type(provider)
    return KindStatus(kind="storage", state="active", plugin=cls.__module__, detail=cls.__qualname__)


def _backend_row() -> KindStatus:
    backend = tai42_app.backends.backend
    if backend is None:
        return KindStatus(kind="backend", state="off", plugin=None, detail="no backend provider installed")
    cls = type(backend)
    return KindStatus(kind="backend", state="active", plugin=cls.__module__, detail=cls.__qualname__)


def _channels_row() -> KindStatus:
    names = tai42_app.channels.names()
    if not names:
        return KindStatus(kind="channels", state="off", plugin=None, detail="no channels registered")
    return KindStatus(kind="channels", state="active", plugin=None, detail="channels: " + ", ".join(names))


def _webhook_verifiers_row() -> KindStatus:
    # Read through the concrete ``AdminFacet`` so the typed manifest accessor narrows
    # ``webhook_verifier_modules`` to ``list[str]``; the contract ``AppAdmin`` exposes
    # only the model-dumped ``dict[str, Any]`` live manifest, which would leak ``Any``.
    modules = cast("AdminFacet", tai42_app.admin).live_manifest_typed.webhook_verifier_modules
    if not modules:
        return KindStatus(
            kind="webhook_verifiers",
            state="off",
            plugin=None,
            detail="no webhook verifiers configured",
        )
    return KindStatus(
        kind="webhook_verifiers",
        state="active",
        plugin=None,
        detail="modules: " + ", ".join(modules),
    )


def _config_row() -> KindStatus:
    mode = config_mode()
    if mode == "file":
        return KindStatus(
            kind="config",
            state="default",
            plugin=None,
            detail="file — built-in default config provider",
        )
    return KindStatus(kind="config", state="active", plugin=None, detail=f"mode: {mode}")


def _studio_plugins_row() -> KindStatus:
    try:
        registry = current_registry()
    except StudioPluginError:
        return KindStatus(
            kind="studio_plugins",
            state="off",
            plugin=None,
            detail="studio plugin registry not built",
        )
    names = sorted(registry.plugins)
    if not names:
        return KindStatus(kind="studio_plugins", state="off", plugin=None, detail="0 plugins")
    return KindStatus(
        kind="studio_plugins",
        state="active",
        plugin=None,
        detail=f"{len(names)} plugin(s): " + ", ".join(names),
    )


# The DB-backed feature gates: each is a kind whose ``off`` state is the honest
# answer when no store is configured. ``(kind, is-configured predicate, enabling
# env var)`` — the predicate reads the same fresh pydantic-settings the feature
# gates on, so the row tracks the live config after a reload. Access control is
# deliberately absent: its off-state already surfaces through the ``identity`` row.
_GATED_FEATURES: list[tuple[str, Callable[[], bool], str]] = [
    ("tool_runs", tool_runs_store_configured, "TAI_TOOL_RUNS_REDIS_URL"),
    ("interactions", interactions_store_configured, "INTERACTIONS_REDIS_URL"),
    ("rate_limit", lambda: bool(RateLimitSettings().redis.redis_url), "TAI_RATE_LIMIT_REDIS_URL"),
    ("marketplace_store", marketplace_store_configured, "MARKETPLACE_STORE_PG_PASSWORD"),
    ("tool_meta", tool_meta_store_configured, "TOOL_META_STORE_PG_PASSWORD"),
    ("connectors", connectors_store_configured, "CONNECTOR_STORE_PG_PASSWORD"),
    ("versioning", versioned_store_configured, "VERSIONING_STORE_PG_PASSWORD"),
]


def _gated_feature_row(kind: str, configured: bool, enabling_var: str) -> KindStatus:
    """One DB-backed feature's live status: ``active`` when its store is configured,
    ``off`` (a legal, reported state — never an error) when it is not, naming the env
    var that turns it on."""
    if configured:
        return KindStatus(kind=kind, state="active", plugin=None, detail=f"{enabling_var} configured")
    return KindStatus(kind=kind, state="off", plugin=None, detail=f"{enabling_var} not configured")


def collect_kind_status() -> list[KindStatus]:
    """Snapshot every pluggable kind's live status, read-only.

    Nine pluggable-kind rows (identity, accounts, monitoring, storage, backend,
    channels, webhook verifiers, config, studio plugins) plus one row per DB-backed
    gated feature (tool_runs, interactions, rate_limit, marketplace store, tool_meta,
    connectors, versioning) whose ``off`` state is the honest answer when no store is
    configured. Reads the process/app registries and the feature settings as they
    stand; each row is ``active``, ``default``, or ``off``. The only swallowed error
    is the documented not-built :class:`StudioPluginError` (reported as an ``off``
    studio-plugins row); every other error propagates so a broken registry is loud.
    """
    return [
        _identity_row(),
        _accounts_row(),
        _monitoring_row(),
        _storage_row(),
        _backend_row(),
        _channels_row(),
        _webhook_verifiers_row(),
        _config_row(),
        _studio_plugins_row(),
        *(_gated_feature_row(kind, configured(), var) for kind, configured, var in _GATED_FEATURES),
    ]


def warn_if_noop_monitoring(rows: list[KindStatus], log: logging.Logger) -> None:
    """Emit the once-per-process NoOp-monitoring warning when the monitoring row is
    ``default`` (NoOp is the active backend). A no-op after the first warning and
    when a real recorder is installed, so a monitoring-less deployment warns exactly
    once across boots/reloads and a configured deployment never warns."""
    global _NOOP_WARNED
    if _NOOP_WARNED:
        return
    monitoring = next(row for row in rows if row.kind == "monitoring")
    if monitoring.state == "default":
        _NOOP_WARNED = True
        log.warning(_NOOP_MONITORING_WARNING)
