import contextlib
import logging
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

import fastmcp
from fastmcp import FastMCP
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.http import StarletteWithLifespan, create_sse_app
from fastmcp.server.server import Transport
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.connectors.store import ConnectorTokenStore
from tai42_contract.manifest import TaiMCPConfig
from tai42_contract.storage import Storage

from tai42_skeleton.agent.binding import AgentBinding
from tai42_skeleton.app.channels_facet import ChannelsFacet
from tai42_skeleton.app.clients import ClientsFacet
from tai42_skeleton.app.conversations_facet import ConversationsFacet
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
    StorageFacet,
    SubAppFacet,
    ToolMetaFacet,
    ToolsFacet,
    VersioningFacet,
    WebhookVerifiersFacet,
)
from tai42_skeleton.app.http import HttpSurface
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.app.route_registry import MOUNT_METHODS, route_registry
from tai42_skeleton.app.sessions import ReloadRejectionMiddleware, SessionRegistry, SessionTrackingMiddleware
from tai42_skeleton.app.sub_mcp_app import SubMcpAppRouter
from tai42_skeleton.backend.registry import BackendHolder
from tai42_skeleton.backup import BackupRegistry, register_core_sections
from tai42_skeleton.channels.registry import ChannelRegistry
from tai42_skeleton.config import ConfigManagerFactory
from tai42_skeleton.extensions import ExtensionRegistry
from tai42_skeleton.middleware.audit_log import AuditLogMiddleware
from tai42_skeleton.middleware.body_limit import BodyLimitMiddleware
from tai42_skeleton.middleware.rate_limit import RateLimitMiddleware
from tai42_skeleton.presets.base_tool_config import (
    PresetInputSchemaSupportRegistry,
    PresetRegistrationTierRegistry,
)
from tai42_skeleton.presets.manager import PresetManager
from tai42_skeleton.presets.seeds import PresetSeedRegistry
from tai42_skeleton.presets.write_validators import PresetWriteValidatorRegistry
from tai42_skeleton.sandbox import SandboxHolder
from tai42_skeleton.settings.audit_log import audit_log_settings
from tai42_skeleton.storage import StorageRegistry
from tai42_skeleton.template import ResourceManager
from tai42_skeleton.tools import ToolRefsRegistry, ToolRegistry, ToolRetryRegistry
from tai42_skeleton.tools.binding import ToolBinding
from tai42_skeleton.tools.rename_referees import ToolRenameRefereeRegistry
from tai42_skeleton.webhooks.registry import WebhookVerifierRegistry

if TYPE_CHECKING:
    from fastmcp.tools import Tool
    from tai42_contract.access_control.identity import IdentityProvider
    from tai42_contract.app import TaiApp
    from tai42_contract.connectors.models import ResolvedConnectionAuth
    from tai42_contract.conversations import DeliveryReceipt
    from tai42_contract.interactions.models import LocationElement, MediaItem
    from tai42_contract.presets import PresetStore
    from tai42_contract.tool_meta import ToolMetaStore

    from tai42_skeleton.manifest import Manifest
    from tai42_skeleton.versioning.store import PostgresVersionedStore

logger = logging.getLogger(__name__)


async def _internal_error_handler(request: Request, exc: Exception) -> Response:
    """Uniform 500 for an unexpected application exception on any adapter route.

    Registered as the base app's ``Exception`` handler so ``ServerErrorMiddleware``
    invokes it on BOTH serving paths (``http_app`` / ``sse_app``). It mints a
    correlation ``error_id``, logs the traceback under it, and returns the generic
    envelope — internal exception text (hosts, paths, stack frames) never reaches the
    client. The id is stamped on the exception so the embedding factory's dispatch
    net, which sees the same exception re-raised by ``ServerErrorMiddleware``,
    correlates its own log line and skips a duplicate response.
    """
    error_id = uuid4().hex
    logger.error(
        "unhandled application error [error_id=%s] on %s %s",
        error_id,
        request.method,
        request.url.path,
        exc_info=exc,
    )
    # Attach the id to the arbitrary raised exception so the embedding factory's
    # dispatch net reads it off the re-raised instance. A slotted exception with no
    # ``__dict__`` cannot accept the stamp; the id already rides the response body
    # and the log line, so a failed stamp must never break this last-resort handler.
    with contextlib.suppress(AttributeError):
        exc.error_id = error_id  # pyright: ignore[reportAttributeAccessIssue]
    return JSONResponse({"error": "Internal Server Error", "error_id": error_id}, status_code=500)


def record_streamable_http_surface(path: str, *, stateless: bool) -> None:
    """Record the streamable-http transport endpoint as a mounted, credential-gated
    surface, so the registry describes it instead of leaving its GETs to the Studio SPA
    catch-all (which matches every path and would charge MCP traffic to the public root
    family and audit it unauthenticated).

    A STATELESS deployment binds no GET on the endpoint — there is no session to stream
    notifications from — so its GETs genuinely do fall through to the catch-all and stay
    a public door. Statelessness alone decides the method set; naming the three methods
    the protocol uses only under-claims anything else the endpoint answers, which stays
    the catch-all's."""
    methods = ["POST", "DELETE"] if stateless else ["GET", "POST", "DELETE"]
    route_registry.record_mounted(
        path=path,
        methods=methods,
        name="mcp_streamable_http",
        summary="MCP streamable-http transport endpoint",
    )


def record_sse_surface(sse_path: str, message_path: str) -> None:
    """Record the SSE transport's two surfaces as mounted, credential-gated ones (see
    :func:`record_streamable_http_surface`): the ``GET`` event stream, and the message
    endpoint, which is a Starlette ``Mount`` and therefore serves everything BENEATH its
    prefix — the client posts to ``<prefix>/?session_id=...`` — never the bare prefix."""
    route_registry.record_mounted(
        path=sse_path,
        methods=["GET"],
        name="mcp_sse_stream",
        summary="MCP SSE transport event stream",
    )
    route_registry.record_mounted(
        path=f"{message_path.rstrip('/')}/{{path:path}}",
        methods=MOUNT_METHODS,
        name="mcp_sse_messages",
        summary="MCP SSE transport message endpoint",
    )


class ServingCore:
    """The per-epoch serving surface: a FRESH FastMCP server plus the feature
    collaborators registered onto it.

    A settings-profile apply builds a NEW ``ServingCore`` off to the side under the
    proposed env and — only on a successful build — makes it the live epoch's core;
    the failed build is discarded untouched. A fresh FastMCP per epoch is
    MANDATORY: its ``_lifespan_manager`` is ref-counted and its route table snapshots
    once, so a reload-added router only serves when the epoch's fresh FastMCP builds a
    fresh ``http_app`` off the new route table.

    The collaborators are constructed with the persistent ``TaiMCP`` (not this core):
    each reads the live serving generation back through the app's per-epoch forwarding
    properties, so a build-in-progress registers into THIS core and the swapped-in
    epoch is what every later read resolves to.
    """

    def __init__(
        self,
        app: "TaiMCP",
        *,
        args: tuple[Any, ...],
        auth: TokenVerifier | None,
        kwargs: dict[str, Any],
    ) -> None:
        # ``on_duplicate="error"`` (server-wide) makes a duplicate registration raise
        # instead of warn-then-replace: every legitimate rebind removes the name
        # first, so an in-boot duplicate is always a genuine collision. Auth is read
        # FRESH per epoch, so a profile that flips ACCESS_CONTROL_* rebuilds the
        # adapter and its verifier chain.
        self._fast_mcp: FastMCP = FastMCP(*args, on_duplicate="error", auth=auth, **kwargs)

        # Active-MCP-session registry + the middleware that captures live sessions on
        # every incoming message, so the list_changed broadcast primitive sees every
        # connected client. Protocol-level infra, registered on the raw server.
        self._session_registry = SessionRegistry()
        self._fast_mcp.add_middleware(SessionTrackingMiddleware(self._session_registry))
        # A session ``tools/call`` is a run surface too: reject it with the same
        # retriable "reloading" error while the reload gate is held.
        self._fast_mcp.add_middleware(ReloadRejectionMiddleware(reload_gate))
        # Tool-edge authorization for projected operations, on the main server AND (in
        # ``_build_sub_app``) every sub-MCP mount. Imported locally to avoid an import
        # cycle (authz -> operations -> app -> server).
        from tai42_skeleton.authz.middleware import AuthzMiddleware

        self._fast_mcp.add_middleware(AuthzMiddleware(app))
        # Synchronous turn budget for this MCP edge: an MCP ``tools/call`` dispatches to
        # ``Tool.run`` directly, never the ``ToolBinding.run_tool`` seam, so it arms the
        # budget itself. Added INNERMOST (after authz/reload) so a denied or rejected call
        # never opens a window.
        from tai42_skeleton.tools.turn_budget import TurnBudgetMiddleware

        self._fast_mcp.add_middleware(TurnBudgetMiddleware())
        # Same reason the budget arms itself here: an MCP ``tools/call`` never reaches the
        # ``ToolBinding.run_tool`` seam, so the ambient invoked-tool is armed at this edge too.
        from tai42_skeleton.app.sub_mcp_app import InvocationSeamMiddleware

        self._fast_mcp.add_middleware(InvocationSeamMiddleware())

        # Per-feature impl collaborators — the bodies behind the facets.
        self._tool_binding = ToolBinding(app)
        self._agent_binding = AgentBinding(app)
        self._backend_holder = BackendHolder()
        # The scalar sandbox-provider holder, rebuilt per epoch beside the backend
        # holder: a reload re-imports the ``sandbox_module`` and re-runs its
        # ``register_sandbox`` against a clean holder. No launch wiring — a sandbox is
        # not launched at boot; sessions are created on demand by consumers.
        self._sandbox_holder = SandboxHolder()
        self._http_surface = HttpSurface(app)
        # Every PUBLIC door (any route registered authed=False, wherever it comes from) is
        # exposed by design; its flood limiter is registered on EVERY epoch's surface here,
        # so it is always on and never left to a manifest opt-in an operator could forget.
        # It derives its coverage from the route registry and no-ops for every authed or
        # unregistered path; budgets are tunable per family via TAI_RATE_LIMIT_*.
        self._http_surface.middleware(RateLimitMiddleware)

        # Webhook-verifier + channel registries, reset each start() so a reload
        # re-imports the manifest's modules and re-registers cleanly.
        self._webhook_verifier_registry = WebhookVerifierRegistry()
        self._channel_registry = ChannelRegistry()

        # Per-base-tool preset write-validator registry, reset each start() so a
        # reload re-imports the tool modules and re-registers cleanly.
        self._write_validator_registry = PresetWriteValidatorRegistry()

        # Per-base-tool preset input-schema support + registration-tier registries,
        # reset each start() alongside the write-validator registry so a reload
        # re-imports the tool modules and re-registers cleanly.
        self._input_schema_support_registry = PresetInputSchemaSupportRegistry()
        self._registration_tier_registry = PresetRegistrationTierRegistry()

        # Per-tool declared tool-references registry, reset each start() so a reload
        # re-imports the tool modules and re-registers cleanly. Mirrors the
        # write-validator registry above.
        self._tool_refs_registry = ToolRefsRegistry()

        # Per-tool declared retry-policy registry (@app.tools.tool(retry=...)),
        # reset each start() for the same reload reason.
        self._tool_retry_registry = ToolRetryRegistry()

        # Tool-rename referee registry + declared-preset-seed registry, reset each
        # start() alongside the registries above so a reload re-imports the plugin
        # modules (and re-arms the platform-internal referees) cleanly. The referee
        # collection gates every rename; the seeds drive the startup/reload applier.
        self._rename_referee_registry = ToolRenameRefereeRegistry()
        self._seed_registry = PresetSeedRegistry()

        # The backup registry is the host's first consumer of its own AppBackup facet:
        # the core host sections are registered here (never on reload, which keeps this
        # object), so the duplicate-name guard is never tripped.
        self._backup_registry = BackupRegistry()
        register_core_sections(self._backup_registry)

        # The preset register/reload engine, rehydrated per epoch from the store by the
        # startup/reload handler.
        self._preset_manager = PresetManager(app)

        # Per-epoch generation state, reached through the app's forwarding properties so
        # a build populates THIS core and a failed build is discarded with it untouched.
        # ``None`` manifest is the pre-boot no-op contract the registration
        # decorators truthiness-check; the registries/maps are empty-but-valid until
        # ``start()`` rebuilds them from the manifest.
        self._manifest: Manifest | None = None
        self._tool_registry: ToolRegistry = ToolRegistry(set[str](), {})
        self._extension_registry: ExtensionRegistry = ExtensionRegistry(frozenset[str]())
        # Manifest MCP servers that failed their viability check (title -> "unavailable"),
        # the tools each live MCP bound (per title, so a targeted reload replaces cleanly),
        # and the per-title names a scoped MCP (re)bind refused because a preset owns them.
        self._failed_mcps: dict[str, str] = {}
        self._mcp_bound_tools: dict[str, set[str]] = {}
        self._mcp_preset_conflicts: dict[str, set[str]] = {}
        # Cached resource manager: dropped each start() so a reload rebuilds it against
        # the freshly-imported storage provider rather than pinning the previous pool.
        self._resource_manager_cache: ResourceManager | None = None

        # The identity/accounts providers this epoch instantiated ONCE at build time
        # (``probe_identity_provider``), keyed by configured name. The live verifier and
        # the accounts-provider routes resolve THIS epoch's instances here rather than
        # re-instantiating per request or reading a plugin module holder — so a failed
        # build's providers are GC'd with the discarded core and never leak.
        self.active_auth_providers: dict[str, IdentityProvider] = {}


class TaiMCP(TaiMCPLifecycleMixin):
    """The concrete ``tai42_contract.app.TaiApp`` impl — owns the FastMCP server and
    exposes the contract facet namespaces as its SOLE feature/contract surface;
    the concrete server additionally exposes a launch surface (``sse_app`` /
    ``http_app`` / ``run`` and friends) that is not part of the facade.

    This class is the composition root only: each feature's impl body lives in
    its feature package (``tools.binding.ToolBinding``, ``agent.binding
    .AgentBinding``, ``backend.registry.BackendHolder``, the extension/monitoring
    registries, ``app.http.HttpSurface``) and each facet forwards straight to its
    collaborator. Callers reach the app's features only through the facets
    (``app.tools.run_tool``, ``app.backends.backend``, ...) or the ``tai42_app``
    handle, never a flat member."""

    def __init__(self, *args, **kwargs):
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

        # The eager boot-scaffold core: an un-booted app (bare construction, an
        # embedded read, the boot-time ``http_app`` build) needs a live serving
        # surface before an epoch is installed. ``app_context`` promotes it to
        # epoch 0; a reload replaces it with a freshly-built one.
        self._building = self._build_serving_core()

    # -- Per-epoch serving core (fresh FastMCP + collaborators) ----------------

    def _build_serving_core(self) -> ServingCore:
        """Build a fresh ``ServingCore`` under the CURRENT env — a fresh FastMCP and
        the feature collaborators registered onto it, with the access-control adapter
        read fresh unless a construction-time ``auth`` pins it. Imported locally to
        avoid an import cycle (access_control -> ... -> app.server)."""
        from tai42_skeleton.access_control.adapter import AuthAdapter
        from tai42_skeleton.access_control.settings import access_control_settings

        if self._auth_override is not None:
            auth: TokenVerifier | None = self._auth_override
        else:
            settings = access_control_settings()
            auth = AuthAdapter(settings) if settings.enable else None
        return ServingCore(self, args=self._server_args, auth=auth, kwargs=self._server_kwargs)

    # -- Facet namespaces (tai42_contract.app.TaiApp) --------------------------

    @property
    def tools(self) -> ToolsFacet:
        return self._tools_facet

    @property
    def agents(self) -> AgentsFacet:
        return self._agents_facet

    @property
    def backends(self) -> BackendsFacet:
        return self._backends_facet

    @property
    def sandboxes(self) -> SandboxesFacet:
        return self._sandboxes_facet

    @property
    def storage(self) -> StorageFacet:
        return self._storage_facet

    @property
    def connectors(self) -> ConnectorsFacet:
        return self._connectors_facet

    @property
    def interactions(self) -> InteractionsFacet:
        return self._interactions_facet

    @property
    def accounts(self) -> AccountsFacet:
        return self._accounts_facet

    @property
    def webhook_verifiers(self) -> WebhookVerifiersFacet:
        return self._webhook_verifiers_facet

    @property
    def channels(self) -> ChannelsFacet:
        return self._channels_facet

    @property
    def conversations(self) -> ConversationsFacet:
        return self._conversations_facet

    @property
    def monitoring(self) -> MonitoringFacet:
        return self._monitoring_facet

    @property
    def extensions(self) -> ExtensionsFacet:
        return self._extensions_facet

    @property
    def http(self) -> HttpFacet:
        return self._http_facet

    @property
    def clients(self) -> ClientsFacet:
        return self._clients

    @property
    def lifecycle(self) -> LifecycleFacet:
        return self._lifecycle_facet

    @property
    def admin(self) -> AdminFacet:
        return self._admin_facet

    @property
    def config(self) -> ConfigFacet:
        return self._config_facet

    @property
    def sub_app(self) -> SubAppFacet:
        return self._sub_app_facet

    @property
    def backup(self) -> BackupFacet:
        return self._backup_facet

    @property
    def versioning(self) -> VersioningFacet:
        return self._versioning_facet

    @property
    def presets(self) -> PresetsFacet:
        return self._presets_facet

    @property
    def tool_meta(self) -> ToolMetaFacet:
        """The tool-metadata overlay facet (folders + per-tool rows) — the
        ``tai42_contract.app.AppToolMeta`` namespace."""
        return self._tool_meta_facet

    # -- Raw FastMCP escape hatch (skeleton-only, ungoverned) ----------------

    @property
    def fastmcp(self) -> FastMCP:
        """The raw, ungoverned FastMCP server — the escape hatch beneath the
        facets.

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
        sub-MCP namespace; ``mcp`` here would read as the sub-MCP."""
        return self._fast_mcp

    async def emit_list_changed(self, kind: str) -> None:
        """Broadcast a ``list_changed`` notification to every active MCP session
        for the given SINGULAR registry ``kind`` (``tool`` / ``prompt`` /
        ``resource``). The generic in-process registration-mutation path (e.g. a
        dev's runtime ``add_prompt`` via ``app.fastmcp``) awaits this after its
        own registry mutation; the reload path drives the same registry from its
        sync scheduler."""
        await self._session_registry.emit_list_changed(kind)

    # -- Live server-surface members (concrete launch surface, not facets) ----

    @property
    def _live_manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        return self._manifest.live_manifest.model_dump(mode="json", exclude_none=True)

    def _base_middleware(self, middleware: list[Middleware] | None) -> list[Middleware]:
        """The app's own base-app middleware, outermost first, ahead of whatever the
        launch surface's caller passes.

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
        finalize wrapper: an over-cap escape (``_BodyTooLarge``) has to reach
        BodyLimitMiddleware and become a 413 before any error handler commits a 500.
        RateLimitMiddleware, by contrast, rejects before the app is entered, so it
        stays an outer finalize wrapper.

        With access control DISABLED no ResourceGuard runs to bind the secret-read
        capability, yet gate-off makes every caller the synthetic admin; the outermost
        entry here binds that capability TRUE for the request so a gate-off caller reaches
        an admin-fenced primitive exactly as ``resolve_caller`` admits it. Gate ON, the
        adapter's ResourceGuard owns the bind and this entry is absent.
        """
        # Local import mirrors ``_build_serving_core``: access_control -> ... -> app.server
        # is a cycle, so the adapter surface is reached lazily here too.
        from tai42_skeleton.access_control.middleware import DisabledAccessControlSecretCapabilityMiddleware
        from tai42_skeleton.access_control.settings import access_control_settings

        gate_off_secret_capability = (
            [] if access_control_settings().enable else [Middleware(DisabledAccessControlSecretCapabilityMiddleware)]
        )
        audit = [Middleware(AuditLogMiddleware)] if audit_log_settings().enable else []
        return [*gate_off_secret_capability, *audit, Middleware(BodyLimitMiddleware), *(middleware or [])]

    def sse_app(
        self,
        path: str | None = None,
        message_path: str | None = None,
        middleware: list[Middleware] | None = None,
    ) -> StarletteWithLifespan:
        actual_path = path if path is not None else "/sse"
        actual_message_path = message_path if message_path is not None else "/messages"

        base_app = create_sse_app(
            server=self._fast_mcp,
            sse_path=actual_path,
            message_path=actual_message_path,
            auth=self._fast_mcp.auth,
            middleware=self._base_middleware(middleware),
        )
        record_sse_surface(actual_path, actual_message_path)

        # Install the uniform-500 handler on the base app's own ServerErrorMiddleware
        # so every adapter route answers the generic {"error", "error_id"} envelope
        # instead of a plain-text 500 with internal detail.
        base_app.add_exception_handler(Exception, _internal_error_handler)

        return self._http_surface.finalize(base_app)

    def http_app(
        self,
        path: str | None = None,
        middleware: list[Middleware] | None = None,
        json_response: bool | None = None,
        stateless_http: bool | None = None,
        transport: Literal["http", "streamable-http", "sse"] = "http",
    ) -> StarletteWithLifespan:

        base_app = self._fast_mcp.http_app(
            path=path,
            middleware=self._base_middleware(middleware),
            json_response=json_response,
            stateless_http=stateless_http,
            transport=transport,
        )
        # Record what THIS build actually mounted, resolving ``path``/``stateless_http``
        # exactly as fastmcp just did (an omitted argument falls back to its process-wide
        # setting), so the registry names the transport paths this deployment serves and
        # no other.
        if transport == "sse":
            record_sse_surface(path if path is not None else fastmcp.settings.sse_path, fastmcp.settings.message_path)
        else:
            record_streamable_http_surface(
                path if path is not None else fastmcp.settings.streamable_http_path,
                stateless=stateless_http if stateless_http is not None else fastmcp.settings.stateless_http,
            )

        # Install the uniform-500 handler on the base app's own ServerErrorMiddleware
        # so every adapter route answers the generic {"error", "error_id"} envelope
        # instead of a plain-text 500 with internal detail.
        base_app.add_exception_handler(Exception, _internal_error_handler)

        return self._http_surface.finalize(base_app)

    async def run_async(
        self, transport: Transport | None = None, show_banner: bool = True, **transport_kwargs: Any
    ) -> None:
        await self._fast_mcp.run_async(transport, show_banner, **transport_kwargs)

    def run(self, transport: Transport | None = None, show_banner: bool = True, **transport_kwargs: Any) -> None:
        self._fast_mcp.run(transport, show_banner, **transport_kwargs)

    async def run_backend(self, args) -> None:
        await self._backend_holder.launch(args)

    # -- Storage / resources -------------------------------------------------

    @property
    def _resource_manager(self) -> ResourceManager:
        if not self._resource_manager_cache:
            self._resource_manager_cache = ResourceManager(self._storage_registry.provider)
        return self._resource_manager_cache

    def _register_storage(self, cls: type[Storage] | None = None):
        return self._storage_registry.register_storage(cls)

    # -- Connectors (AppConnectors facet body) -------------------------------

    def _register_connector(self, descriptor: ProviderDescriptor) -> None:
        """Register a connector provider from its pure descriptor data.

        Forwarded by the ``tai42_app.connectors`` handle for every manifest
        ``connectors`` entry during boot/reload registration. A connector is pure
        data, so this is a plain call, not a decorator — it stores the descriptor
        in the engine registry."""
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
        ``headers`` channels; ``None`` maps to ``None``."""
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
        params: dict[str, str] | None = None,
        form: dict[str, Any] | None = None,
        attachments: "list[MediaItem] | None" = None,
        location: "LocationElement | None" = None,
    ) -> str:
        from tai42_skeleton.conversations import accept

        return await accept(
            channel,
            our_identity,
            client_address,
            cap_key,
            text,
            provider_message_id,
            params=params,
            form=form,
            attachments=attachments,
            location=location,
        )

    async def _conversation_record_delivery_status(
        self, channel: str, provider_message_id: str, status: "DeliveryReceipt"
    ) -> None:
        from tai42_skeleton.conversations import record_delivery_status

        await record_delivery_status(channel, provider_message_id, status)

    # -- Versioning + presets seams --------------------------------------------
    # ``app.versioning.store`` and ``app.presets.store`` forward here; ``bind`` is
    # the kernel every preset builds its live tool through.

    @property
    def _versioned_store(self) -> "PostgresVersionedStore":
        # Concretely typed (not the ``VersionedStore`` protocol) so the batched
        # ``list_active_bodies`` accessor — a concrete-only method — resolves
        # through this reference. The ``app.versioning.store`` facet re-narrows to
        # the protocol for the contract surface.
        from tai42_skeleton.versioning import versioned_store

        return versioned_store()

    @property
    def preset_manager(self) -> PresetManager:
        """The preset register/reload engine (spec map + quarantine set + register/
        reload/remove/rehydrate). Skeleton-only surface — like ``emit_list_changed``
        and ``fastmcp``, it is deliberately not on the ``tai42_contract.app.TaiApp``
        protocol; the preset routes and the startup/reload rehydration hook reach
        it through this concrete instance."""
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
        output_schema: dict[str, Any] | None = None,
        input_schema: dict[str, Any] | None = None,
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
        )

    # -- Lifecycle seam --------------------------------------------------------

    def _mcp_tools(self, config: TaiMCPConfig, tools):
        # The mixin's re-init path binds remote-MCP tools through this seam.
        self._tool_binding.mcp_tools(config, tools)
