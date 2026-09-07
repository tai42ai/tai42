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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel
from tai42_contract.access_control.registry import get_identity_provider_factory_staged
from tai42_contract.accounts.registry import iter_accounts_provider_factories_staged
from tai42_contract.app import tai42_app
from tai42_kit.db import component_binding, component_store_configured, database_password_env

from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.config.config_mode import config_mode
from tai42_skeleton.connectors.providers.registry import list_providers_staged
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.interactions.settings import interactions_store_configured
from tai42_skeleton.monitoring.noop import NoOpMonitoring
from tai42_skeleton.monitoring.registry import get_monitoring_staged
from tai42_skeleton.plugins.registry import StudioPluginError, current_registry_staged
from tai42_skeleton.routers.tool_runs_settings import tool_runs_store_configured
from tai42_skeleton.settings.rate_limit import RateLimitSettings
from tai42_skeleton.states.db import STATES_COMPONENT, states_store_configured

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
        get_identity_provider_factory_staged(name)
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
    names = [name for name, _factory in iter_accounts_provider_factories_staged()]
    if not names:
        return KindStatus(kind="accounts", state="off", plugin=None, detail="no accounts provider registered")
    return KindStatus(
        kind="accounts",
        state="active",
        plugin=", ".join(names),
        detail="providers: " + ", ".join(names),
    )


def _monitoring_row() -> KindStatus:
    # Staged read so the build-time summary + noop warning reflect the generation
    # being built (a build that adds a recorder is not reported OFF); serve-time reads
    # fall through to the committed backend.
    backend = get_monitoring_staged()
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


def _sandbox_row() -> KindStatus:
    sandbox = tai42_app.sandboxes.sandbox
    if sandbox is None:
        return KindStatus(kind="sandbox", state="off", plugin=None, detail="no sandbox provider installed")
    cls = type(sandbox)
    return KindStatus(kind="sandbox", state="active", plugin=cls.__module__, detail=cls.__qualname__)


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
        # Staged read so the build-time summary reflects the generation being built;
        # serve-time reads fall through to the committed registry.
        registry = current_registry_staged()
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


@dataclass(frozen=True)
class GatedFeature:
    """One DB-backed feature gate's registration — the single source both the live
    ``kinds`` table and the generated OFF-behavior doc read.

    ``kind`` is the pluggable-kind name the row reports; ``label`` is the human name
    the docs table shows; ``configured`` is the predicate reading the same fresh
    pydantic-settings the feature gates on, so the row tracks the live config after a
    reload; ``enabling_var`` is the env var that turns the feature on, live-derived
    from the component's binding so it tracks a rebind; ``off_behavior`` is the
    one-line uniform-OFF contract the feature honors when its store is absent.
    ``enabling_var`` reads env only, not a store, so a doc generator calls it under a
    clean env to print the default name without booting a store.
    """

    kind: str
    label: str
    configured: Callable[[], bool]
    enabling_var: Callable[[], str]
    off_behavior: str


# The DB-backed feature gates: each is a kind whose ``off`` state is the honest
# answer when no store is configured. Access control is deliberately absent: its
# off-state already surfaces through the ``identity`` row.
_GATED_FEATURES: list[GatedFeature] = [
    GatedFeature(
        kind="tool_runs",
        label="Background tool runs",
        configured=tool_runs_store_configured,
        enabling_var=lambda: "TAI_TOOL_RUNS_REDIS_URL",
        off_behavior=(
            "Run reads answer 200 empty (a single run reads 404); submit refuses 501 tool-runs-not-configured."
        ),
    ),
    GatedFeature(
        kind="interactions",
        label="Interactions + notifications",
        configured=interactions_store_configured,
        enabling_var=lambda: "INTERACTIONS_REDIS_URL",
        off_behavior=(
            "The notification feed and stream refuse 501 interactions-not-configured "
            "(ask_user refuses as a tool error); feed reads answer 200 empty."
        ),
    ),
    GatedFeature(
        kind="rate_limit",
        label="Rate limiting",
        configured=lambda: bool(RateLimitSettings().redis.redis_url),
        enabling_var=lambda: "TAI_RATE_LIMIT_REDIS_URL",
        off_behavior=(
            "Pass-through — every public door (any route registered authed=False) is unthrottled; one boot WARNING."
        ),
    ),
    GatedFeature(
        kind="marketplace_store",
        label="Marketplace store",
        configured=lambda: component_store_configured(SKELETON_COMPONENT),
        enabling_var=lambda: database_password_env(component_binding(SKELETON_COMPONENT)),
        off_behavior=(
            "Installed and advisory reads answer 200 empty; the registry-proxy catalog is "
            "store-independent (502 against an unreachable registry); install and attribution "
            "writes refuse 501 marketplace-not-configured."
        ),
    ),
    GatedFeature(
        kind="tool_meta",
        label="Tool-metadata overlay",
        configured=lambda: component_store_configured(SKELETON_COMPONENT),
        enabling_var=lambda: database_password_env(component_binding(SKELETON_COMPONENT)),
        off_behavior="Folder and tag reads answer 200 empty; writes refuse 501 tool-meta-not-configured.",
    ),
    GatedFeature(
        kind="connectors",
        label="Connectors store",
        configured=lambda: component_store_configured(SKELETON_COMPONENT),
        enabling_var=lambda: database_password_env(component_binding(SKELETON_COMPONENT)),
        off_behavior="Connector endpoints answer OFF; writes refuse 501 connectors-not-configured.",
    ),
    GatedFeature(
        kind="versioning",
        label="Versioning",
        configured=lambda: component_store_configured(SKELETON_COMPONENT),
        enabling_var=lambda: database_password_env(component_binding(SKELETON_COMPONENT)),
        off_behavior="Preset and version reads answer 200 empty or 404; writes refuse 501 versioning-not-configured.",
    ),
    GatedFeature(
        kind="states",
        label="Subject state records",
        configured=states_store_configured,
        enabling_var=lambda: database_password_env(component_binding(STATES_COMPONENT)),
        off_behavior="Every states read and write refuses 501 states-not-configured.",
    ),
]


def _gated_feature_row(feature: GatedFeature) -> KindStatus:
    """One DB-backed feature's live status: ``active`` when its store is configured,
    ``off`` (a legal, reported state — never an error) when it is not, naming the env
    var that turns it on."""
    var = feature.enabling_var()
    if feature.configured():
        return KindStatus(kind=feature.kind, state="active", plugin=None, detail=f"{var} configured")
    return KindStatus(kind=feature.kind, state="off", plugin=None, detail=f"{var} not configured")


def _connectors_row(feature: GatedFeature) -> KindStatus:
    """The connectors feature row: the shared store-configured gate, with the count of
    registered providers appended so the table shows how many providers are wired even
    when the store — and thus the connectors surface — is off."""
    row = _gated_feature_row(feature)
    return row.model_copy(update={"detail": f"{row.detail}, {len(list_providers_staged())} provider(s)"})


def collect_kind_status() -> list[KindStatus]:
    """Snapshot every pluggable kind's live status, read-only.

    Ten pluggable-kind rows (identity, accounts, monitoring, storage, backend,
    sandbox, channels, webhook verifiers, config, studio plugins) plus one row per DB-backed
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
        _sandbox_row(),
        _channels_row(),
        _webhook_verifiers_row(),
        _config_row(),
        _studio_plugins_row(),
        *(
            _connectors_row(feature) if feature.kind == "connectors" else _gated_feature_row(feature)
            for feature in _GATED_FEATURES
        ),
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
