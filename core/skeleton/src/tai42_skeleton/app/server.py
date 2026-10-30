"""The concrete ``tai42_contract.app.TaiApp`` server (``TaiMCP``)."""

from typing import TYPE_CHECKING, Any, Literal, cast

import fastmcp
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan, create_sse_app
from fastmcp.server.server import Transport
from starlette.middleware import Middleware
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.connectors.store import ConnectorTokenStore
from tai42_contract.manifest import TaiMCPConfig
from tai42_contract.storage import Storage
from tai42_contract.template import TemplatedText

from tai42_skeleton.access_control.adapter import AuthAdapter
from tai42_skeleton.access_control.middleware import DisabledAccessControlSecretCapabilityMiddleware
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.app.channels_facet import ChannelsFacet
from tai42_skeleton.app.clients import ClientsFacet
from tai42_skeleton.app.conversations_facet import ConversationsFacet
from tai42_skeleton.app.error_envelope import install_error_handlers
from tai42_skeleton.app.facets import (
    AccountsFacet,
    AdminFacet,
    AgentsFacet,
    BackendsFacet,
    BackupFacet,
    ConfigFacet,
    ConnectorsFacet,
    ExtensionsFacet,
    HttpFacet,
    InteractionsFacet,
    LifecycleFacet,
    MonitoringFacet,
    PresetsFacet,
    SandboxesFacet,
    StatesFacet,
    StorageFacet,
    SubAppFacet,
    ToolMetaFacet,
    ToolsFacet,
    VersioningFacet,
    WebhookVerifiersFacet,
)
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
from tai42_skeleton.app.media_facet import MediaFacet
from tai42_skeleton.app.serving_core import ServingCore, record_sse_surface, record_streamable_http_surface
from tai42_skeleton.app.sub_mcp_app import SubMcpAppRouter
from tai42_skeleton.app.transport_stream import TransportStreamDrainExemptMiddleware, bind_sse_session_ender
from tai42_skeleton.config import ConfigManagerFactory
from tai42_skeleton.middleware.audit_log import AuditLogMiddleware
from tai42_skeleton.middleware.body_limit import BodyLimitMiddleware
from tai42_skeleton.presets.manager import PresetManager
from tai42_skeleton.settings.audit_log import audit_log_settings
from tai42_skeleton.storage import StorageRegistry
from tai42_skeleton.template import ResourceManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from fastmcp.server.auth import TokenVerifier
    from fastmcp.tools import Tool
    from tai42_contract.app import PendingMessage, TaiApp
    from tai42_contract.channels import SendOutcome
    from tai42_contract.connectors.models import ResolvedConnectionAuth
    from tai42_contract.conversations import AnswerPart, DeliveryReceipt, InboundMediaKind, InboundRejectionReason
    from tai42_contract.interactions.models import IngestedMedia, LocationElement, MediaItem, MediaOrigin
    from tai42_contract.presets import PresetStore
    from tai42_contract.tool_meta import ToolMetaStore

    from tai42_skeleton.versioning.store import PostgresVersionedStore


class TaiMCP(TaiMCPLifecycleMixin):
    """The concrete ``tai42_contract.app.TaiApp`` impl — owns the FastMCP server.

    Exposes the contract facet namespaces as its SOLE feature/contract surface;
    the concrete server additionally exposes a launch surface (``sse_app`` /
    ``http_app`` / ``run`` and friends) that is not part of the facade.

    This class is the composition root only: each feature's impl body lives in
    its feature package (``tools.binding.ToolBinding``, ``agent.binding
    .AgentBinding``, ``backend.registry.BackendHolder``, the extension/monitoring
    registries, ``app.http.HttpSurface``) and each facet forwards straight to its
    collaborator. Callers reach the app's features only through the facets
    (``app.tools.run_tool``, ``app.backends.backend``, ...) or the ``tai42_app``
    handle, never a flat member.
    """

    def __init__(self, *args, **kwargs):
        """Capture the FastMCP construction ``args``/``kwargs`` (``auth`` pulled aside) and build the facets.

        The facet namespaces and an eager boot-scaffold serving core are created so a freshly
        constructed, un-booted app already has a serving surface.
        """
        super().__init__()
        # FastMCP construction params captured for the per-epoch ``ServingCore``
        # build. ``auth`` is pulled aside: production reads it FRESH per epoch from
        # access-control settings, while a test-supplied ``auth`` overrides that
        # so a fixed verifier can be pinned across a throwaway app's epochs.
        self._server_args: tuple[Any, ...] = args
        self._auth_override: TokenVerifier | None = kwargs.pop("auth", None)
        self._server_kwargs: dict[str, Any] = kwargs

        # The half-built core during a build, and otherwise ``None`` so reads resolve
        # to the live epoch's core. Set to the eager boot-scaffold core at the end of
        # construction so a freshly-built (un-booted) app still has a serving surface.
        self._building: ServingCore | None = None

        self._storage_registry: StorageRegistry = StorageRegistry()
        self._config_manager = ConfigManagerFactory.create()
        self._mcp_sub_app_router: SubMcpAppRouter = SubMcpAppRouter(app=self)
        self._clients: ClientsFacet = ClientsFacet()

        # The contract facet namespaces, partitioning the feature surface.
        self._tools_facet = ToolsFacet(self)
        self._agents_facet = AgentsFacet(self)
        self._backends_facet = BackendsFacet(self)
        self._sandboxes_facet = SandboxesFacet(self)
        self._storage_facet = StorageFacet(self)
        self._connectors_facet = ConnectorsFacet(self)
        self._interactions_facet = InteractionsFacet(self)
        self._accounts_facet = AccountsFacet(self)
        self._webhook_verifiers_facet = WebhookVerifiersFacet(self)
        self._channels_facet = ChannelsFacet(self)
        self._conversations_facet = ConversationsFacet(self)
        self._media_facet = MediaFacet(self)
        self._monitoring_facet = MonitoringFacet(self)
        self._extensions_facet = ExtensionsFacet(self)
        self._http_facet = HttpFacet(self)
        self._lifecycle_facet = LifecycleFacet(self)
        self._admin_facet = AdminFacet(self)
        self._config_facet = ConfigFacet(self)
        self._sub_app_facet = SubAppFacet(self)
        self._backup_facet = BackupFacet(self)
        self._versioning_facet = VersioningFacet(self)
        self._presets_facet = PresetsFacet(self)
        self._tool_meta_facet = ToolMetaFacet(self)
        self._states_facet = StatesFacet(self)

        # The eager boot-scaffold core: an un-booted app (bare construction, an
        # embedded read, the boot-time ``http_app`` build) needs a live serving
        # surface before an epoch is installed. ``app_context`` promotes it to
        # epoch 0; a reload replaces it with a freshly-built one.
        self._building = self._build_serving_core()

    # -- Per-epoch serving core (fresh FastMCP + collaborators) ----------------

    def _build_serving_core(self) -> ServingCore:
        """Build a fresh ``ServingCore`` under the CURRENT env.

        A fresh FastMCP and the feature collaborators registered onto it, with the access-control
        adapter read fresh unless a construction-time ``auth`` pins it.
        """
        if self._auth_override is not None:
            auth: TokenVerifier | None = self._auth_override
        else:
            settings = access_control_settings()
            auth = AuthAdapter(settings) if settings.enable else None
        return ServingCore(self, args=self._server_args, auth=auth, kwargs=self._server_kwargs)

    # -- Facet namespaces (tai42_contract.app.TaiApp) --------------------------

    @property
    def tools(self) -> ToolsFacet:
        """The tool/toolkit registration, lookup, and execution facet (``app.tools``)."""
        return self._tools_facet

    @property
    def agents(self) -> AgentsFacet:
        """The agent registration and run-binding facet (``app.agents``)."""
        return self._agents_facet

    @property
    def backends(self) -> BackendsFacet:
        """The background-execution backend facet (``app.backends``)."""
        return self._backends_facet

    @property
    def sandboxes(self) -> SandboxesFacet:
        """The sandbox-provider facet (``app.sandboxes``)."""
        return self._sandboxes_facet

    @property
    def storage(self) -> StorageFacet:
        """The resource-storage facet (``app.storage``)."""
        return self._storage_facet

    @property
    def connectors(self) -> ConnectorsFacet:
        """The connector registration and credential-resolution facet (``app.connectors``)."""
        return self._connectors_facet

    @property
    def interactions(self) -> InteractionsFacet:
        """The interactions facet (``app.interactions``) — the ``ask`` facade."""
        return self._interactions_facet

    @property
    def accounts(self) -> AccountsFacet:
        """The identity/accounts provider facet (``app.accounts``)."""
        return self._accounts_facet

    @property
    def webhook_verifiers(self) -> WebhookVerifiersFacet:
        """The webhook-verifier registration facet (``app.webhook_verifiers``)."""
        return self._webhook_verifiers_facet

    @property
    def channels(self) -> ChannelsFacet:
        """The channel registration facet (``app.channels``)."""
        return self._channels_facet

    @property
    def conversations(self) -> ConversationsFacet:
        """The conversation-bridge facet (``app.conversations``)."""
        return self._conversations_facet

    @property
    def media(self) -> MediaFacet:
        """The served-media ingestion facet (``app.media``)."""
        return self._media_facet

    @property
    def monitoring(self) -> MonitoringFacet:
        """The monitoring-backend facet (``app.monitoring``)."""
        return self._monitoring_facet

    @property
    def extensions(self) -> ExtensionsFacet:
        """The extension registration facet (``app.extensions``)."""
        return self._extensions_facet

    @property
    def http(self) -> HttpFacet:
        """The custom HTTP route facet (``app.http``)."""
        return self._http_facet

    @property
    def clients(self) -> ClientsFacet:
        """The pooled-client lifecycle facet (``app.clients``)."""
        return self._clients

    @property
    def lifecycle(self) -> LifecycleFacet:
        """The startup/shutdown/reload lifecycle facet (``app.lifecycle``)."""
        return self._lifecycle_facet

    @property
    def admin(self) -> AdminFacet:
        """The in-process admin-operations facet (``app.admin``)."""
        return self._admin_facet

    @property
    def config(self) -> ConfigFacet:
        """The process config-manager facet (``app.config``)."""
        return self._config_facet

    @property
    def sub_app(self) -> SubAppFacet:
        """The sub-app MCP router facet (``app.sub_app``)."""
        return self._sub_app_facet

    @property
    def backup(self) -> BackupFacet:
        """The backup-section facet (``app.backup``)."""
        return self._backup_facet

    @property
    def versioning(self) -> VersioningFacet:
        """The versioned-document store facet (``app.versioning``)."""
        return self._versioning_facet

    @property
    def presets(self) -> PresetsFacet:
        """The presets facet (``app.presets``)."""
        return self._presets_facet

    @property
    def states(self) -> StatesFacet:
        """The subject-keyed state store facet — the ``tai42_contract.app.AppStates`` namespace."""
        return self._states_facet

    @property
    def tool_meta(self) -> ToolMetaFacet:
        """The tool-metadata overlay facet — the ``tai42_contract.app.AppToolMeta`` namespace.

        The organizational overlay (folders + per-tool rows) over any live tool.
        """
        return self._tool_meta_facet

    # -- Raw FastMCP escape hatch (skeleton-only, ungoverned) ----------------

    @property
    def fastmcp(self) -> FastMCP:
        """The raw, ungoverned FastMCP server — the escape hatch beneath the facets.

        Prefer the facets; reach here only for what the facets don't wrap
        (prompts, resources, ``add_middleware``, sampling, elicit-handlers,
        completions, server metadata such as ``name``/``version``/``auth``, the
        process-global ``fastmcp.settings``). Anything registered THROUGH this
        server skips the platform's governance: manifest gating, the extension
        registry's ``validate()``, and the access-control gate. It is
        deliberately NOT on the ``tai42_contract.app.TaiApp`` protocol — the
        contract stays FastMCP-free so an alternative impl remains possible, so
        this accessor is skeleton-specific.

        Named ``fastmcp`` (not ``mcp``) because ``app.sub_app`` already owns the
        sub-MCP namespace; ``mcp`` here would read as the sub-MCP.
        """
        return self._fast_mcp

    async def emit_list_changed(self, kind: str) -> None:
        """Broadcast a ``list_changed`` notification to every active MCP session for the given registry ``kind``.

        ``kind`` is SINGULAR (``tool`` / ``prompt`` / ``resource``). The generic in-process
        registration-mutation path (e.g. a dev's runtime ``add_prompt`` via ``app.fastmcp``) awaits
        this after its own registry mutation; the reload path drives the same registry from its
        sync scheduler.
        """
        await self._session_registry.emit_list_changed(kind)

    # -- Live server-surface members (concrete launch surface, not facets) ----

    @property
    def _live_manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        return self._manifest.live_manifest.model_dump(mode="json", exclude_none=True)

    def _base_middleware(
        self, middleware: list[Middleware] | None, *, stream_path: str, carries_replies: bool
    ) -> list[Middleware]:
        """The app's own base-app middleware, outermost first, ahead of whatever the launch surface's caller passes.

        FastMCP builds the base app's stack as ``[*auth middleware, *middleware]``, so
        everything returned here runs AFTER the access-control gate has resolved the
        caller — which is what lets AuditLogMiddleware read the bound identity instead
        of re-resolving it. It wraps the body cap, so an over-cap 413 is audited like
        any other outcome. It is the one entry the operator can switch off
        (``TAI_AUDIT_LOG_ENABLE=false``), and off means absent from this list — no
        registered no-op.

        The app-level body-size cap is the backstop on EVERY route (authed routes
        read their bodies unbounded otherwise); always on, tune via
        TAI_BODY_LIMIT_MAX_BODY_BYTES. It MUST sit inside the base app's own
        Starlette stack (its own ``ServerErrorMiddleware``), not as an outer
        finalize wrapper: an over-cap escape (``_BodyTooLargeError``) has to reach
        BodyLimitMiddleware and become a 413 before any error handler commits a 500.
        RateLimitMiddleware, by contrast, rejects before the app is entered, so it
        stays an outer finalize wrapper.

        With access control DISABLED no ResourceGuard runs to bind the secret-read
        capability, yet gate-off makes every caller the synthetic admin; the outermost
        entry here binds that capability TRUE for the request so a gate-off caller reaches
        an admin-fenced primitive exactly as ``resolve_caller`` admits it. Gate ON, the
        adapter's ResourceGuard owns the bind and this entry is absent.

        ``stream_path`` is the route path the transport serves its server-to-client stream
        on; that stream is exempt from its generation's retire drain
        (:class:`~tai42_skeleton.app.transport_stream.TransportStreamDrainExemptMiddleware`).
        ``carries_replies`` says whether that stream carries its session's replies (the SSE
        transport's does), so each call counts as answered once its reply is written there.
        """
        gate_off_secret_capability = (
            [] if access_control_settings().enable else [Middleware(DisabledAccessControlSecretCapabilityMiddleware)]
        )
        audit = [Middleware(AuditLogMiddleware)] if audit_log_settings().enable else []
        return [
            *gate_off_secret_capability,
            *audit,
            Middleware(BodyLimitMiddleware),
            Middleware(TransportStreamDrainExemptMiddleware, stream_path=stream_path, carries_replies=carries_replies),
            *(middleware or []),
        ]

    def sse_app(
        self,
        path: str | None = None,
        message_path: str | None = None,
        middleware: list[Middleware] | None = None,
    ) -> StarletteWithLifespan:
        """Build the SSE ASGI app for this server, recording the served SSE surface.

        The message endpoint is mounted and announced with a trailing slash (a supplied
        ``message_path`` gains one): the mount matches the announced address, so no message is
        redirected to an absolute address a TLS-ending proxy would rewrite.
        """
        actual_path = path if path is not None else "/sse"
        actual_message_path = f"{(message_path if message_path is not None else '/messages').rstrip('/')}/"

        base_app = create_sse_app(
            server=self._fast_mcp,
            sse_path=actual_path,
            message_path=actual_message_path,
            auth=self._fast_mcp.auth,
            middleware=self._base_middleware(middleware, stream_path=actual_path, carries_replies=True),
        )
        # Its sessions end with the lifespan that owns it (its generation's close).
        bind_sse_session_ender(base_app, actual_message_path)
        record_sse_surface(actual_path, actual_message_path)

        # Install the shared error-envelope handlers on the base app's own middleware
        # so every adapter route answers the generic {"error", "error_id"} envelope
        # instead of a plain-text 500 with internal detail, and a reload plugin
        # boot-abort reaches the caller.
        install_error_handlers(base_app)

        return self._http_surface.finalize(base_app)

    def http_app(
        self,
        path: str | None = None,
        middleware: list[Middleware] | None = None,
        json_response: bool | None = None,
        stateless_http: bool | None = None,
        transport: Literal["http", "streamable-http", "sse"] = "http",
    ) -> StarletteWithLifespan:
        """Build the streamable-HTTP (or SSE) ASGI app for this server, recording the served surface."""
        # Resolve ``path`` exactly as fastmcp does (an omitted argument falls back to its
        # process-wide setting), so the stream exemption and the registry name the transport
        # path this deployment serves and no other.
        if transport == "sse":
            stream_path = path if path is not None else fastmcp.settings.sse_path
        else:
            stream_path = path if path is not None else fastmcp.settings.streamable_http_path
        base_app = self._fast_mcp.http_app(
            path=path,
            middleware=self._base_middleware(middleware, stream_path=stream_path, carries_replies=transport == "sse"),
            json_response=json_response,
            stateless_http=stateless_http,
            transport=transport,
        )
        # Record what THIS build actually mounted, resolving ``stateless_http`` the same way.
        if transport == "sse":
            # Its sessions end with the lifespan that owns it (its generation's close).
            bind_sse_session_ender(base_app, fastmcp.settings.message_path)
            record_sse_surface(stream_path, fastmcp.settings.message_path)
        else:
            record_streamable_http_surface(
                stream_path,
                stateless=stateless_http if stateless_http is not None else fastmcp.settings.stateless_http,
            )

        # Install the shared error-envelope handlers on the base app's own middleware
        # so every adapter route answers the generic {"error", "error_id"} envelope
        # instead of a plain-text 500 with internal detail, and a reload plugin
        # boot-abort reaches the caller.
        install_error_handlers(base_app)

        return self._http_surface.finalize(base_app)

    async def run_async(
        self, transport: Transport | None = None, show_banner: bool = True, **transport_kwargs: Any
    ) -> None:
        """Run the FastMCP server asynchronously over ``transport``."""
        await self._fast_mcp.run_async(transport, show_banner, **transport_kwargs)

    def run(self, transport: Transport | None = None, show_banner: bool = True, **transport_kwargs: Any) -> None:
        """Run the FastMCP server (blocking) over ``transport``."""
        self._fast_mcp.run(transport, show_banner, **transport_kwargs)

    async def run_backend(self, args) -> None:
        """Launch the background-execution backend with ``args``."""
        await self._backend_holder.launch(args)

    # -- Storage / resources -------------------------------------------------

    @property
    def _resource_manager(self) -> ResourceManager:
        if not self._resource_manager_cache:
            self._resource_manager_cache = ResourceManager(
                self._storage_registry.provider, on_evicted=self._fire_template_evicted
            )
        return self._resource_manager_cache

    def _register_storage(self, cls: type[Storage] | None = None):
        return self._storage_registry.register_storage(cls)

    # -- Connectors (AppConnectors facet body) -------------------------------

    def _register_connector(self, descriptor: ProviderDescriptor) -> None:
        """Register a connector provider from its pure descriptor data.

        Forwarded by the ``tai42_app.connectors`` handle for every manifest
        ``connectors`` entry during boot/reload registration. A connector is pure
        data, so this is a plain call, not a decorator — it stores the descriptor
        in the engine registry.
        """
        from tai42_skeleton.connectors.providers.registry import register_connector

        register_connector(descriptor)

    @property
    def _token_store(self) -> ConnectorTokenStore:
        return self._connector_token_store()

    @staticmethod
    def _connector_token_store() -> ConnectorTokenStore:
        from tai42_skeleton.connectors.store import token_store

        return token_store()

    async def _resolve_connection_auth(
        self, connection_id: str, provider_id: str, sub_service: str
    ) -> "ResolvedConnectionAuth | None":
        """The ``app.connectors.resolve_connection_auth`` facade body.

        Fail-close chokepoint: reads the bound execution identity FIRST and refuses
        BEFORE any resolution when none is bound, so an identity-less door never gets
        the operator's service token injected. The mapping wraps every credential value
        in ``SecretStr``, conveying the OAuth ``access_token`` plus static ``env`` /
        ``headers`` channels; ``None`` maps to ``None``.
        """
        from pydantic import SecretStr
        from tai42_contract.connectors.models import ResolvedConnectionAuth

        from tai42_skeleton.authz.execution_identity import get_execution_identity
        from tai42_skeleton.connectors.runtime.resolver import resolve_managed_auth

        if get_execution_identity() is None:
            raise RuntimeError(
                "resolve_connection_auth refuses to inject a connection credential with no execution "
                "identity bound: an identity-less door must never receive the operator's credential"
            )

        managed = await resolve_managed_auth(connection_id, provider_id, sub_service, allow_refresh=True)
        if managed is None:
            return None
        return ResolvedConnectionAuth(
            access_token=SecretStr(managed.access_token) if managed.access_token is not None else None,
            env={key: SecretStr(value) for key, value in managed.env.items()},
            headers={key: SecretStr(value) for key, value in managed.headers.items()},
        )

    # -- Conversations (AppConversations facet body) --------------------------
    # ``app.conversations`` forwards here; the bridge core lives in its own module
    # (like the connector registry), reached through a deferred import so the app
    # package never imports the conversations module at construction.

    async def _conversation_accept(
        self,
        channel: str,
        our_identity: str,
        client_address: str,
        cap_key: str,
        text: str,
        provider_message_id: str,
        medium_message_id: str | None,
        reply_to_medium_message_id: str | None,
        params: dict[str, str] | None = None,
        form: dict[str, Any] | None = None,
        attachments: "list[MediaItem] | None" = None,
        location: "LocationElement | None" = None,
        locale: str | None = None,
        form_tag: str | None = None,
    ) -> str:
        from tai42_skeleton.conversations import accept

        return await accept(
            channel,
            our_identity,
            client_address,
            cap_key,
            text,
            provider_message_id,
            medium_message_id,
            reply_to_medium_message_id,
            params=params,
            form=form,
            attachments=attachments,
            location=location,
            locale=locale,
            form_tag=form_tag,
        )

    async def _conversation_record_delivery_status(
        self, channel: str, provider_message_id: str, status: "DeliveryReceipt", *, errors: Any = None
    ) -> None:
        from tai42_skeleton.conversations import record_delivery_status

        await record_delivery_status(channel, provider_message_id, status, errors=errors)

    async def _conversation_send_recorded(
        self,
        *,
        channel: str,
        our_identity: str,
        client_address: str,
        content: "AnswerPart",
        send: "Callable[[], Awaitable[SendOutcome]]",
    ) -> "SendOutcome":
        from tai42_skeleton.conversations import send_recorded

        return await send_recorded(
            channel=channel,
            our_identity=our_identity,
            client_address=client_address,
            content=content,
            send=send,
        )

    async def _conversation_pending_messages(self, thread_id: str, *, after: str) -> "list[PendingMessage]":
        from tai42_skeleton.conversations import pending_messages

        return await pending_messages(thread_id, after=after)

    async def _conversation_notify_inbound_rejected(
        self,
        *,
        channel_id: str,
        recipient: str,
        sender_identity: str,
        kind: str,
        reason: "InboundRejectionReason",
    ) -> None:
        from tai42_skeleton.channels.inbound_rejection import notify_inbound_rejected

        await notify_inbound_rejected(
            channel_id=channel_id,
            recipient=recipient,
            sender_identity=sender_identity,
            kind=kind,
            reason=reason,
        )

    # -- Media (AppMedia facet body) ------------------------------------------
    # ``app.media`` forwards here; the ingestion chokepoint lives in its own module,
    # reached through a deferred import so the app package never imports it at construction.

    async def _media_ingest_media(
        self,
        *,
        source: "AsyncIterator[bytes]",
        kind_hint: "InboundMediaKind | str | None",
        declared_mime: str | None,
        filename: str | None,
        declared_size: int | None,
        origin: "MediaOrigin",
    ) -> "IngestedMedia":
        from tai42_skeleton.interactions.media_ingest import ingest_media

        return await ingest_media(
            source=source,
            kind_hint=kind_hint,
            declared_mime=declared_mime,
            filename=filename,
            declared_size=declared_size,
            origin=origin,
        )

    async def _media_bind_media(self, media_id: str, *, origin: "MediaOrigin") -> "IngestedMedia":
        from tai42_skeleton.interactions.media_ingest import bind_media

        return await bind_media(media_id, origin=origin)

    # -- Versioning + presets seams --------------------------------------------
    # ``app.versioning.store`` and ``app.presets.store`` forward here; ``bind`` is
    # the kernel every preset builds its live tool through.

    @property
    def _versioned_store(self) -> "PostgresVersionedStore":
        # Concretely typed (not the ``VersionedStore`` protocol) so the batched
        # ``list_active_version_and_body`` accessor — a concrete-only method — resolves
        # through this reference. The ``app.versioning.store`` facet re-narrows to
        # the protocol for the contract surface.
        from tai42_skeleton.versioning import versioned_store

        return versioned_store()

    @property
    def preset_manager(self) -> PresetManager:
        """The preset register/reload engine (spec map + quarantine set + register/reload/remove/rehydrate).

        Skeleton-only surface — like ``emit_list_changed`` and ``fastmcp``, it is deliberately not on
        the ``tai42_contract.app.TaiApp`` protocol; the preset routes and the startup/reload
        rehydration hook reach it through this concrete instance.
        """
        return self._preset_manager

    @property
    def _preset_store(self) -> "PresetStore":
        from tai42_skeleton.presets import preset_store

        # Wire the engine's collision predicate so ``create_preset`` raises
        # ``PresetNameConflictError`` BEFORE any store write when a name collides
        # with a live non-preset base tool.
        return preset_store(name_conflicts=self._preset_manager.name_conflicts)

    @property
    def _tool_meta_store(self) -> "ToolMetaStore":
        from tai42_skeleton.tool_meta import tool_meta_store

        return tool_meta_store()

    async def _preset_bind(
        self,
        base_tool: str,
        fixed_kwargs: dict[str, Any],
        *,
        name: str,
        description: str = "",
        output_schema: TemplatedText | dict[str, Any] | None = None,
        input_schema: TemplatedText | dict[str, Any] | None = None,
        input_map: TemplatedText | str | None = None,
        output_map: TemplatedText | str | None = None,
    ) -> "Tool":
        from tai42_skeleton.presets import preset_bind

        # The concrete app IS a structural ``TaiApp`` (asserted by the conformance
        # test); the cast bridges pyright's nominal facet-return-type variance.
        return await preset_bind(
            cast("TaiApp", self),
            base_tool,
            fixed_kwargs,
            name=name,
            description=description,
            output_schema=output_schema,
            input_schema=input_schema,
            input_map=input_map,
            output_map=output_map,
        )

    # -- Lifecycle seam --------------------------------------------------------

    def _mcp_tools(self, config: TaiMCPConfig, tools, preset_names: frozenset[str]):
        # The mixin's re-init path binds remote-MCP tools through this seam.
        self._tool_binding.mcp_tools(config, tools, preset_names)
