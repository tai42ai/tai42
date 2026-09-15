"""External identity/credential provider facets: accounts and connectors."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tai42_contract.access_control.identity import IdentityProvider
from tai42_contract.connectors.models import ResolvedConnectionAuth
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.connectors.store import ConnectorTokenStore


@runtime_checkable
class AppAccounts(Protocol):
    """Read access to the CURRENT epoch's live identity/accounts provider instances.

    An accounts-provider plugin ships login routes that need the SAME provider instance
    the epoch built and probed (its resolved config, cached discovery/JWKS, injected
    settings). Rather than a module-level holder — which a failed epoch build would
    leave pointing at a half-built generation — the plugin's routes resolve the live
    instance here. The contract exposes only this read; the runtime forwards it to the
    current epoch, so the contract never learns about epochs.
    """

    def active_provider(self, name: str) -> IdentityProvider | None:
        """The provider the CURRENT epoch instantiated under ``name``, or ``None`` when none is active.

        An ``AccountsProvider`` is an ``IdentityProvider``. ``None`` means no provider is
        active under that name — the name is not configured, or a build is mid-flight.
        """
        ...


@runtime_checkable
class AppConnectors(Protocol):
    """Registration and credential resolution for connector providers."""

    def register_connector(self, descriptor: ProviderDescriptor) -> None:
        """Register a connector provider from its pure descriptor data.

        Called for every manifest ``connectors`` entry at boot/reload, and by any
        code holding the handle (a connector is pure data, so this is a plain call,
        not a decorator).
        """
        ...

    @property
    def token_store(self) -> ConnectorTokenStore:
        """The connector token store (single-namespace, keyed by ``connection_id``)."""
        ...

    async def resolve_connection_auth(
        self, connection_id: str, provider_id: str, sub_service: str
    ) -> ResolvedConnectionAuth | None:
        """Resolve the credential a connection injects, for the CURRENT caller.

        The facade accessor an in-process plugin uses to read a skeleton-resolved
        credential (OAuth token / static env / static headers) without importing the
        skeleton — refreshing an expired OAuth token under the connection lock. Async
        because resolution performs I/O (the OAuth refresh under the connection lock);
        callers ``await`` it. Returns ``None`` when the connection injects nothing.

        GUARANTEE (enforced by the skeleton implementation): (1) it RAISES a loud
        constant-message error when NO execution identity is bound — the fail-close
        raise fires as the awaited coroutine runs, BEFORE resolving, so an
        identity-less door never gets creds injected; and
        (2) ``connection_id`` is a REFERENCE supplied by operator settings, NEVER
        session-supplied, so a session can neither reach an identity-less door's
        creds nor name another connection. The contract carries no logic — the
        skeleton owns the fail-close enforcement.
        """
        ...
