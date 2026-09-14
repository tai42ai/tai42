"""The provider/registry facades (``app.agents``/``backends``/``sandboxes``/``storage``/
``monitoring``/``extensions``/``webhook_verifiers``/``connectors``/``accounts``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_skeleton.extensions.registry import extension_name

from .base import _Facet

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any, TypeVar

    from tai42_contract.access_control.identity import IdentityProvider
    from tai42_contract.agent import Agent
    from tai42_contract.backend import Backend
    from tai42_contract.connectors.models import ResolvedConnectionAuth
    from tai42_contract.connectors.providers import ProviderDescriptor
    from tai42_contract.connectors.store import ConnectorTokenStore
    from tai42_contract.extensions import ExtensionKind
    from tai42_contract.manifest import ExtensionElement
    from tai42_contract.monitoring import Monitoring
    from tai42_contract.sandbox import Sandbox, SandboxPolicy
    from tai42_contract.storage import Storage
    from tai42_contract.webhooks import WebhookVerifier

    from tai42_skeleton.template import ResourceManager

    _AgentT = TypeVar("_AgentT", bound=Agent)


class AgentsFacet(_Facet):
    """``app.agents`` — agent registration + lookup (``AppAgents``)."""

    def agent(
        self, name: str, tags: set[str] | None = None, meta: dict[str, Any] | None = None
    ) -> Callable[[type[_AgentT]], type[_AgentT]]:
        return self._app._agent_binding.agent(name, tags, meta)

    def get_agent(self, name: str) -> Agent:
        return self._app._agent_binding.get_agent(name)

    def all_agents(self) -> dict[str, Agent]:
        return self._app._agent_binding.all_agents()


class BackendsFacet(_Facet):
    """``app.backends`` — backend registration (``AppBackends``)."""

    def register_backend(self, cls: type | None = None) -> Callable[..., Any]:
        return self._app._backend_holder.register_backend(cls)

    @property
    def backend(self) -> Backend | None:
        return self._app._backend_holder.backend


class SandboxesFacet(_Facet):
    """``app.sandboxes`` — sandbox provider registration + the acquisition chokepoint
    and the resolved-policy read (``AppSandboxes``)."""

    def register_sandbox(self, cls: type[Sandbox]) -> type[Sandbox]:
        return self._app._sandbox_holder.register_sandbox(cls)

    @property
    def sandbox(self) -> Sandbox | None:
        """The registered provider, or ``None`` — status/introspection ONLY. Never gate
        execution on this nullable read; acquire through :meth:`require_sandbox`."""
        return self._app._sandbox_holder.sandbox

    def require_sandbox(self) -> Sandbox:
        """The ONE raising acquisition chokepoint every consumer reaches — returns the
        registered provider or raises ``SandboxUnavailableError`` when none is registered."""
        return self._app._sandbox_holder.require()

    def sandbox_policy(self) -> SandboxPolicy:
        """The skeleton-resolved :class:`SandboxPolicy` — the SAME value the holder binds
        to the kit at provider registration, read through the ONE shared resolver so the
        bound policy and this read can never diverge. Available REGARDLESS of whether a
        provider is registered (it reads operator config, not a provider)."""
        from tai42_skeleton.sandbox.policy import resolve_sandbox_policy

        return resolve_sandbox_policy()


class StorageFacet(_Facet):
    """``app.storage`` — storage provider registration + the resource manager
    layered on it (``AppStorage``)."""

    def register_storage(self, cls: type[Storage] | None = None) -> Callable[..., Any]:
        return self._app._register_storage(cls)

    @property
    def provider(self) -> Storage | None:
        """The registered storage provider, or ``None`` while dead by default.

        The read-only counterpart to :meth:`register_storage`, mirroring
        :attr:`BackendsFacet.backend`: the storage doors report identity + serve
        CRUD off this instance, answering ``None`` as the honest empty state
        rather than fabricating a default provider."""
        return self._app._storage_registry.provider

    @property
    def resource_manager(self) -> ResourceManager:
        """The resource manager layered on the registered storage provider.

        Loads/renders manifest-stored resources (by id, url, or raw file — text or
        media); accessing it before a storage provider is registered raises when a
        stored resource is first touched.
        """
        return self._app._resource_manager


class MonitoringFacet(_Facet):
    """``app.monitoring`` — monitoring backend registration (``AppMonitoring``)."""

    def register_monitoring(self, builder: Callable[..., Any] | None = None) -> Callable[..., Any]:
        from tai42_skeleton.monitoring import register_monitoring

        return register_monitoring(builder)

    @property
    def active(self) -> Monitoring:
        """The active monitoring backend (the no-op default until a plugin
        installs a real one via ``register_monitoring``)."""
        from tai42_skeleton.monitoring import get_monitoring

        return get_monitoring()


class ExtensionsFacet(_Facet):
    """``app.extensions`` — extension registration + listing (``AppExtensions``)."""

    def extension(
        self,
        f: Callable | None = None,
        *,
        kind: ExtensionKind,
        name: str | None = None,
        requires_body_locality: bool = False,
    ) -> Callable[..., Any]:
        return self._app._extension_registry.extension(
            f, kind=kind, name=name, requires_body_locality=requires_body_locality
        )

    def available_extensions(self) -> list[dict]:
        return self._app._extension_registry.available_extensions()

    def validate_combo(self, combo: Sequence[ExtensionElement]) -> None:
        """Validate one extension combo against the LIVE registry: reject an
        unknown extension name and a combo carrying two extensions of a
        non-stackable kind. A combo element is an extension name or a
        ``{"name", "config"}`` mapping — validation keys on the name. Raises
        :class:`~tai42_skeleton.exceptions.exceptions.TaiValidationError`
        on the first violation (the shape both the presets and the tool-extensions
        routes validate a combo through before any persist)."""
        registry = self._app._extension_registry
        available = {entry["name"] for entry in registry.available_extensions()}
        names = [extension_name(element) for element in combo]
        unknown = sorted(name for name in names if name not in available)
        if unknown:
            from tai42_skeleton.exceptions.exceptions import TaiValidationError

            raise TaiValidationError(f"unknown extension(s): {', '.join(unknown)}")
        registry.validate(combo)


class WebhookVerifiersFacet(_Facet):
    """``app.webhook_verifiers`` — webhook-verifier registration + lookup
    (``AppWebhookVerifiers``)."""

    def register(self, name: str, verifier: WebhookVerifier) -> None:
        return self._app._webhook_verifier_registry.register(name, verifier)

    def get(self, name: str) -> WebhookVerifier:
        return self._app._webhook_verifier_registry.get(name)

    def names(self) -> list[str]:
        """The sorted names of every registered verifier — the catalog the Studio
        bind form offers instead of free text. Empty when no verifier lifecycle
        module is loaded."""
        return self._app._webhook_verifier_registry.names()


class ConnectorsFacet(_Facet):
    """``app.connectors`` — connector provider registration + the token store
    (``AppConnectors``)."""

    def register_connector(self, descriptor: ProviderDescriptor) -> None:
        return self._app._register_connector(descriptor)

    @property
    def token_store(self) -> ConnectorTokenStore:
        return self._app._token_store

    async def resolve_connection_auth(
        self, connection_id: str, provider_id: str, sub_service: str
    ) -> ResolvedConnectionAuth | None:
        """Resolve the credential a connection injects for the CURRENT caller — the facade
        accessor an in-process plugin uses to read a skeleton-resolved credential without
        importing the skeleton.

        FAILS CLOSE BEFORE any resolution: reads the bound execution identity FIRST and
        raises a loud, constant-message error when none is bound — so an identity-less
        door (raw agent-run SSE, sync-HTTP/MCP, background/schedule) can never have the
        operator's service token injected. Only with a bound identity does it proceed to
        ``resolve_managed_auth`` (refreshing an expired OAuth token under the connection
        lock), then MAPS the resulting ``ManagedAuth`` onto the contract
        :class:`ResolvedConnectionAuth`, conveying all three channels with every value
        wrapped ``SecretStr``. ``None`` maps to ``None`` (the connection injects nothing).
        ``connection_id`` is a REFERENCE supplied by operator settings, never
        session-supplied, so a session can neither reach an identity-less door's creds nor
        name another identity's connection."""
        return await self._app._resolve_connection_auth(connection_id, provider_id, sub_service)


class AccountsFacet(_Facet):
    """``app.accounts`` — read access to the current epoch's live provider instances
    (``AppAccounts``)."""

    def active_provider(self, name: str) -> IdentityProvider | None:
        # Resolve the CURRENT (live) epoch's provider, never the generation under
        # construction — a failed build's provider instances must never bind into a
        # surviving epoch's memoized verifier (the zero-mutation invariant). See
        # ``TaiMCP._live_serving_core``.
        return self._app._live_serving_core.active_auth_providers.get(name)
