"""The assembled ``TaiApp`` facade.

One ``Protocol`` per feature (see :mod:`tai42_contract.app.facets`), composed into
a single ``TaiApp`` protocol that exposes them as namespaces (``app.tools``,
``app.agents``, ...). The app members are partitioned across the
sub-protocols — each lives in exactly one, save the shared leaf names
``store`` (``versioning`` + ``presets`` + ``tool_meta``) and ``register``/``get``
(``webhook_verifiers`` + ``channels``). The runtime forwarding handle is
``tai42_app`` (see :mod:`tai42_contract.app.handle`).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol, cast, runtime_checkable

from tai42_contract.tools import AppTools

from .facets import (
    AppAccounts,
    AppAdmin,
    AppAgents,
    AppBackends,
    AppBackup,
    AppChannels,
    AppClients,
    AppConfig,
    AppConnectors,
    AppConversations,
    AppExtensions,
    AppHttp,
    AppInteractions,
    AppLifecycle,
    AppMonitoring,
    AppPresets,
    AppSandboxes,
    AppStates,
    AppStorage,
    AppSubApp,
    AppToolMeta,
    AppVersioning,
    AppWebhookVerifiers,
    DeclaredRouteMetadata,
    PendingMessage,
    RouteAction,
)
from .handle import tai42_app as _tai_app_handle


@runtime_checkable
class TaiApp(Protocol):
    """The assembled facade — per-feature sub-protocols exposed as namespaces."""

    @property
    def tools(self) -> AppTools:
        """Tool and toolkit registration and lookup."""
        ...

    @property
    def agents(self) -> AppAgents:
        """Agent registration and access."""
        ...

    @property
    def backends(self) -> AppBackends:
        """Task backend registration and access."""
        ...

    @property
    def sandboxes(self) -> AppSandboxes:
        """Sandbox-provider registration and access."""
        ...

    @property
    def storage(self) -> AppStorage:
        """Storage-provider registration and access."""
        ...

    @property
    def connectors(self) -> AppConnectors:
        """Connector-provider registration and credential resolution."""
        ...

    @property
    def accounts(self) -> AppAccounts:
        """Read access to the current epoch's live identity/accounts providers."""
        ...

    @property
    def webhook_verifiers(self) -> AppWebhookVerifiers:
        """Inbound webhook verifier registration and lookup."""
        ...

    @property
    def channels(self) -> AppChannels:
        """Channel registration and lookup."""
        ...

    @property
    def conversations(self) -> AppConversations:
        """Inbound-message and delivery-status entry surface for medium adapters."""
        ...

    @property
    def monitoring(self) -> AppMonitoring:
        """Monitoring backend registration and access."""
        ...

    @property
    def extensions(self) -> AppExtensions:
        """Tool-extension factory registration and lookup."""
        ...

    @property
    def interactions(self) -> AppInteractions:
        """The ``ask`` interactions facade."""
        ...

    @property
    def http(self) -> AppHttp:
        """HTTP middleware, route registration, and mount introspection."""
        ...

    @property
    def clients(self) -> AppClients:
        """Pooled async client lifecycle for the process."""
        ...

    @property
    def lifecycle(self) -> AppLifecycle:
        """Startup, shutdown, reload, and post-swap lifecycle hook registration."""
        ...

    @property
    def admin(self) -> AppAdmin:
        """In-process admin operations (binding, tool reload, config reload)."""
        ...

    @property
    def config(self) -> AppConfig:
        """Access to the process config manager."""
        ...

    @property
    def backup(self) -> AppBackup:
        """Named backup-section registration and export/import."""
        ...

    @property
    def sub_app(self) -> AppSubApp:
        """Access to the sub-app router."""
        ...

    @property
    def versioning(self) -> AppVersioning:
        """The generic versioned-document store."""
        ...

    @property
    def presets(self) -> AppPresets:
        """The presets namespace (typed view plus bind kernel)."""
        ...

    @property
    def tool_meta(self) -> AppToolMeta:
        """The tool-metadata overlay over any live tool."""
        ...

    @property
    def states(self) -> AppStates:
        """The subject-keyed state store."""
        ...


class _TaiAppRuntime(TaiApp, Protocol):
    """The runtime forwarding handle: the assembled ``TaiApp`` facade plus the two binders.

    The two binders are the startup injection and the scoped bind.
    """

    def bind(self, impl: object) -> None: ...

    def bound(self, impl: object) -> AbstractContextManager[None]: ...


# The forwarding handle, typed as the assembled facade plus the binders so consumers
# get real member types instead of ``__getattr__ -> Any``. ``cast`` is a runtime no-op.
tai42_app: _TaiAppRuntime = cast("_TaiAppRuntime", _tai_app_handle)


__all__ = [
    "AppAccounts",
    "AppAdmin",
    "AppAgents",
    "AppBackends",
    "AppBackup",
    "AppChannels",
    "AppClients",
    "AppConfig",
    "AppConnectors",
    "AppConversations",
    "AppExtensions",
    "AppHttp",
    "AppInteractions",
    "AppLifecycle",
    "AppMonitoring",
    "AppPresets",
    "AppSandboxes",
    "AppStates",
    "AppStorage",
    "AppSubApp",
    "AppToolMeta",
    "AppTools",
    "AppVersioning",
    "AppWebhookVerifiers",
    "DeclaredRouteMetadata",
    "PendingMessage",
    "RouteAction",
    "TaiApp",
    "tai42_app",
]
