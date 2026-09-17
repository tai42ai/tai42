"""Per-feature sub-protocols composed by :class:`~tai42_contract.app.TaiApp`.

One ``Protocol`` per feature area, grouped by subsystem into sibling modules. The app
members are partitioned across these — each member lives in exactly one, save the shared
leaf names: ``store`` (:class:`AppVersioning` + :class:`AppPresets` + :class:`AppToolMeta`)
and ``register`` / ``get`` (:class:`AppWebhookVerifiers` + :class:`AppChannels`).
Vendor return types follow the ``TYPE_CHECKING`` rule.
"""

from __future__ import annotations

from tai42_contract.app.facets.authoring import AppPresets, AppToolMeta, AppVersioning
from tai42_contract.app.facets.execution import (
    AppAgents,
    AppBackends,
    AppExtensions,
    AppSandboxes,
    AppStorage,
)
from tai42_contract.app.facets.integrations import AppAccounts, AppConnectors
from tai42_contract.app.facets.messaging import (
    AppChannels,
    AppConversations,
    AppInteractions,
    AppWebhookVerifiers,
    PendingMessage,
)
from tai42_contract.app.facets.routing import AppHttp, DeclaredRouteMetadata, RouteAction
from tai42_contract.app.facets.runtime import (
    AppAdmin,
    AppBackup,
    AppClients,
    AppConfig,
    AppLifecycle,
    AppMonitoring,
    AppSubApp,
)
from tai42_contract.app.facets.states import AppStates

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
    "AppVersioning",
    "AppWebhookVerifiers",
    "DeclaredRouteMetadata",
    "PendingMessage",
    "RouteAction",
]
