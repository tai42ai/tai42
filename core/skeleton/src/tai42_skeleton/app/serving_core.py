"""The per-epoch serving surface — a fresh FastMCP plus its feature collaborators —
and the recording of its MCP transport surfaces into the route registry."""

from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP
from fastmcp.server.auth import TokenVerifier

from tai42_skeleton.agent.binding import AgentBinding
from tai42_skeleton.app.http import HttpSurface
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.app.route_registry import MOUNT_METHODS, route_registry
from tai42_skeleton.app.sessions import ReloadRejectionMiddleware, SessionRegistry, SessionTrackingMiddleware
from tai42_skeleton.backend.registry import BackendHolder
from tai42_skeleton.backup import BackupRegistry, register_core_sections
from tai42_skeleton.channels.registry import ChannelRegistry
from tai42_skeleton.conversations.target_validators import TargetBindValidatorRegistry
from tai42_skeleton.extensions import ExtensionRegistry
from tai42_skeleton.middleware.rate_limit import RateLimitMiddleware
from tai42_skeleton.presets.base_tool_config import PresetInputSchemaSupportRegistry
from tai42_skeleton.presets.manager import PresetManager
from tai42_skeleton.presets.seeds import PresetSeedRegistry
from tai42_skeleton.presets.write_validators import PresetWriteValidatorRegistry
from tai42_skeleton.sandbox import SandboxHolder
from tai42_skeleton.states.backup import register_states_backup_section
from tai42_skeleton.states.seeds import StateTemplateSeedRegistry
from tai42_skeleton.states.service import (
    StatesAttachReconcilerRegistry,
    StatesAttachValidatorRegistry,
    StatesConsumerListerRegistry,
    StatesService,
)
from tai42_skeleton.tools import ToolRefsRegistry, ToolRegistry, ToolRetryRegistry, ToolTierRegistry
from tai42_skeleton.tools.binding import ToolBinding
from tai42_skeleton.tools.delete_referees import ToolDeleteRefereeRegistry
from tai42_skeleton.tools.detach_referees import StateTemplateDetachRefereeRegistry
from tai42_skeleton.tools.rename_referees import ToolRenameRefereeRegistry
from tai42_skeleton.webhooks.registry import WebhookVerifierRegistry

if TYPE_CHECKING:
    from tai42_contract.access_control.identity import IdentityProvider

    from tai42_skeleton.app.server import TaiMCP
    from tai42_skeleton.manifest import Manifest
    from tai42_skeleton.template import ResourceManager


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
        # Run-time tier fence for this MCP edge: an MCP ``tools/call`` reaches ``Tool.run``
        # directly, never the ``ToolBinding.run_tool`` seam that fences the in-process
        # doors, so this edge enforces the same admin fence for a ``fenced``/``secret``
        # tool. Added before the dispatch scope so a fenced denial never opens a window.
        from tai42_skeleton.tools.tier import ToolTierFenceMiddleware

        self._fast_mcp.add_middleware(ToolTierFenceMiddleware(app))
        # The shared dispatch scope for this MCP edge: an MCP ``tools/call`` dispatches to
        # ``Tool.run`` directly, never the ``ToolBinding.run_tool`` seam, so this edge arms
        # the whole run lifecycle itself (invoked-tool deposit, run-attribution stamp, turn
        # budget, and — for a registered preset — the preset stamp + runs-index row/trace
        # root), entering the SAME ``dispatch_scope`` the in-process seam does. Added
        # INNERMOST (after authz/reload/tier-fence) so a denied or rejected call arms no scope.
        from tai42_skeleton.tools.dispatch_scope import DispatchScopeMiddleware

        self._fast_mcp.add_middleware(DispatchScopeMiddleware(app))

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

        # Per-target-kind conversation-route bind-validator registry, reset each start()
        # so a reload re-imports the plugin modules and re-registers cleanly. Route
        # creation consults it after the target exists, so a target carrying a bind-time
        # defect is refused at create, not at first message.
        self._target_validator_registry = TargetBindValidatorRegistry()

        # Per-base-tool preset input-schema support + registration-tier registries,
        # reset each start() alongside the write-validator registry so a reload
        # re-imports the tool modules and re-registers cleanly. The tier registry is
        # shared by the preset-authoring gate (``app.presets.registration_tier``) and the
        # run-time fence (``app.tools.tier``) — one object, both facets.
        self._input_schema_support_registry = PresetInputSchemaSupportRegistry()
        self._registration_tier_registry = ToolTierRegistry()

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
        self._delete_referee_registry = ToolDeleteRefereeRegistry()
        self._detach_referee_registry = StateTemplateDetachRefereeRegistry()
        self._seed_registry = PresetSeedRegistry()

        # The backup registry is the host's first consumer of its own AppBackup facet:
        # the core host sections are registered here (never on reload, which keeps this
        # object), so the duplicate-name guard is never tripped.
        self._backup_registry = BackupRegistry()
        register_core_sections(self._backup_registry)

        # The subject-keyed state store: one shared service over the record substrate,
        # plus the consumer-owned registries (attach validators, consumer listers, template
        # seeds) reset each start() so a reload re-imports the plugin modules and
        # re-registers cleanly. The service holds refs to the SAME registry objects, so a
        # reset+re-register is visible to it without rebuilding the service.
        self._states_attach_validators = StatesAttachValidatorRegistry()
        self._states_attach_reconcilers = StatesAttachReconcilerRegistry()
        self._states_consumer_listers = StatesConsumerListerRegistry()
        self._states_template_seeds = StateTemplateSeedRegistry()
        self._states_service = StatesService(
            attach_validators=self._states_attach_validators,
            attach_reconcilers=self._states_attach_reconcilers,
            consumer_listers=self._states_consumer_listers,
            seeds=self._states_template_seeds,
        )
        register_states_backup_section(self._backup_registry)

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
