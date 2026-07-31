"""The assembled ``TaiApp`` facade.

One ``Protocol`` per feature (see :mod:`tai42_contract.app.facets`), composed into
a single ``TaiApp`` protocol that exposes them as namespaces (``app.tools``,
``app.agents``, ...). The app members are partitioned across the
sub-protocols — each lives in exactly one, save the shared leaf names
``store`` (``versioning`` + ``presets``) and ``register``/``get``
(``webhook_verifiers`` + ``channels``). The runtime forwarding handle is
``tai42_app`` (see :mod:`tai42_contract.app.handle`).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol, cast, runtime_checkable

from tai42_contract.tools import AppTools

from .facets import (
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
    AppLifecycle,
    AppMonitoring,
    AppPresets,
    AppStorage,
    AppSubApp,
    AppVersioning,
    AppWebhookVerifiers,
    DeclaredRouteMetadata,
    RouteAction,
)
from .handle import tai42_app as _tai_app_handle


@runtime_checkable
class TaiApp(Protocol):
    """The assembled facade — per-feature sub-protocols exposed as namespaces."""

    @property
    def tools(self) -> AppTools: ...

    @property
    def agents(self) -> AppAgents: ...

    @property
    def backends(self) -> AppBackends: ...

    @property
    def storage(self) -> AppStorage: ...

    @property
    def connectors(self) -> AppConnectors: ...

    @property
    def webhook_verifiers(self) -> AppWebhookVerifiers: ...

    @property
    def channels(self) -> AppChannels: ...

    @property
    def conversations(self) -> AppConversations: ...

    @property
    def monitoring(self) -> AppMonitoring: ...

    @property
    def extensions(self) -> AppExtensions: ...

    @property
    def http(self) -> AppHttp: ...

    @property
    def clients(self) -> AppClients: ...

    @property
    def lifecycle(self) -> AppLifecycle: ...

    @property
    def admin(self) -> AppAdmin: ...

    @property
    def config(self) -> AppConfig: ...

    @property
    def backup(self) -> AppBackup: ...

    @property
    def sub_app(self) -> AppSubApp: ...

    @property
    def versioning(self) -> AppVersioning: ...

    @property
    def presets(self) -> AppPresets: ...


class _TaiAppRuntime(TaiApp, Protocol):
    """The runtime forwarding handle: the assembled ``TaiApp`` facade plus the two
    binders — the startup injection and the scoped bind."""

    def bind(self, impl: object) -> None: ...

    def bound(self, impl: object) -> AbstractContextManager[None]: ...


# The forwarding handle, typed as the assembled facade plus the binders so consumers
# get real member types instead of ``__getattr__ -> Any``. ``cast`` is a runtime no-op.
tai42_app: _TaiAppRuntime = cast("_TaiAppRuntime", _tai_app_handle)


__all__ = [
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
    "AppLifecycle",
    "AppMonitoring",
    "AppPresets",
    "AppStorage",
    "AppSubApp",
    "AppTools",
    "AppVersioning",
    "AppWebhookVerifiers",
    "DeclaredRouteMetadata",
    "RouteAction",
    "TaiApp",
    "tai42_app",
]
