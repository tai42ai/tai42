"""Per-feature sub-protocols composed by :class:`~tai42_contract.app.TaiApp`.

One ``Protocol`` per feature area. The app members are partitioned across
these — each member lives in exactly one, save the shared leaf names:
``store`` (:class:`AppVersioning` + :class:`AppPresets`) and ``register`` /
``get`` (:class:`AppWebhookVerifiers` + :class:`AppChannels`). Vendor return
types follow the ``TYPE_CHECKING`` rule.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Protocol,
    TypeVar,
    overload,
    runtime_checkable,
)

from pydantic import BaseModel

from tai42_contract.access_control.identity import IdentityProvider
from tai42_contract.agent import Agent
from tai42_contract.backend import Backend
from tai42_contract.backup import BackupSectionInfo
from tai42_contract.channels import (
    Channel,
    CorrelationStore,
    InboundAnswerOutcome,
    InboundBridge,
)
from tai42_contract.clients import BaseClient
from tai42_contract.config import ConfigManager
from tai42_contract.connectors.models import ResolvedConnectionAuth
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.connectors.store import ConnectorTokenStore
from tai42_contract.conversations import DeliveryReceipt
from tai42_contract.extensions import ExtensionKind
from tai42_contract.interactions.asker import AskUser
from tai42_contract.monitoring import Monitoring
from tai42_contract.presets import PresetInputSchemaSupport, PresetStore, PresetWriteValidator
from tai42_contract.sandbox import Sandbox, SandboxPolicy
from tai42_contract.storage import Storage
from tai42_contract.sub_mcp import SubMcpAppRouter
from tai42_contract.versioning import VersionedStore
from tai42_contract.webhooks import WebhookVerifier

if TYPE_CHECKING:
    from fastmcp.tools import Tool
    from starlette.requests import Request
    from starlette.responses import Response

_AgentT = TypeVar("_AgentT", bound=Agent)
_StorageT = TypeVar("_StorageT", bound=Storage)

#: A route's authorization character — the single class deciding how a route is
#: reached. ``read`` / ``write`` are grantable through a role's per-tag level;
#: ``fenced`` is the admin-only mutation fence and ``secret`` the admin-only
#: bulk-secret read fence, neither opened by any per-tag level. Passed to
#: :meth:`AppHttp.custom_route` as ``action`` (``None`` lets the surface derive
#: the grantable class from the HTTP methods).
RouteAction = Literal["read", "write", "fenced", "secret"]


@dataclass(frozen=True)
class DeclaredRouteMetadata:
    """The behavioral OpenAPI properties a route DECLARES.

    A route registered through the operations adapter supplies this
    from its operation's metadata + declared error classes; a native ``/api/*``
    handler passes it explicitly at its ``custom_route`` registration. Its
    ``reload_gated`` / ``reads_body`` / error statuses / success status feed the
    emitted spec and the coverage/parity gates.

    ``additional_success_statuses`` names further 2xx codes one method may answer
    besides ``success_status``; each is emitted as its own success response.
    """

    reload_gated: bool
    reads_body: bool
    error_statuses: tuple[int, ...]
    success_status: int
    additional_success_statuses: tuple[int, ...] = ()


@runtime_checkable
class AppAgents(Protocol):
    def agent(
        self, name: str, tags: set[str] | None = None, meta: dict[str, Any] | None = None
    ) -> Callable[[type[_AgentT]], type[_AgentT]]:
        """Register an :class:`Agent` subclass under ``name`` and auto-register
        its JSON ``run`` tool.

        ``tags`` are the run tool's native tags, set on its constructed tool object.
        ``meta`` is generic registration metadata threaded onto the same constructed
        run-tool object (naming no consumer concept — a registrant attaches any generic
        ``tai42/*`` key, e.g. a crash-resume flag the run-dispatch seam reads). The
        decorator returns the class unchanged, so the decorated symbol keeps its
        concrete subclass type."""
        ...

    def get_agent(self, name: str) -> Agent:
        """Fetch a registered agent instance by name; raise if missing."""
        ...

    def all_agents(self) -> dict[str, Agent]:
        """Every registered agent keyed by registration name (a shallow copy, so a
        caller iterating it cannot mutate the live registry). The preset bind kernel
        reads the keys to detect an agent base (the run tool binds under the
        registration name)."""
        ...


@runtime_checkable
class AppBackends(Protocol):
    def register_backend(
        self, cls: type[Backend] | None = None
    ) -> Callable[[type[Backend]], type[Backend]] | type[Backend]: ...

    @property
    def backend(self) -> Backend | None: ...


@runtime_checkable
class AppSandboxes(Protocol):
    """The sandbox-provider namespace (``app.sandboxes``)."""

    def register_sandbox(self, cls: type[Sandbox]) -> type[Sandbox]:
        """Register the :class:`Sandbox` provider (decorator form, returning the class
        unchanged). One provider backs the slot; a second registration is a conflict
        the runtime rejects loudly."""
        ...

    @property
    def sandbox(self) -> Sandbox | None:
        """The registered provider, or ``None`` — status/introspection ONLY. Never
        gate execution on this nullable read; acquire through :meth:`require_sandbox`,
        the every-door guarantee (a ``None`` property is how per-consumer checks
        drift)."""
        ...

    def require_sandbox(self) -> Sandbox:
        """The ONE raising acquisition chokepoint: return the registered provider or
        raise :class:`~tai42_contract.sandbox.SandboxUnavailableError` (naming
        ``TAI_MCP_SANDBOX`` and ``sandbox_module``) when none is registered. EVERY
        consumer acquires through here."""
        ...

    def sandbox_policy(self) -> SandboxPolicy:
        """The skeleton-resolved :class:`~tai42_contract.sandbox.SandboxPolicy` — the
        plugin-reachable READ of the SAME policy the skeleton binds to the kit at the
        session-create chokepoint.

        Available REGARDLESS of whether a provider is registered (it reads operator
        config, not a provider). Mirrors the ``ask_user`` / ``resolve_connection_auth``
        facade accessors that let an in-process plugin read a skeleton-resolved value
        without importing the skeleton; a consumer building a policied spec reads the
        network default and ``scrub_transcript`` here. Implemented in the skeleton,
        which resolves the policy once and returns the SAME value it binds to the kit."""
        ...


@runtime_checkable
class AppInteractions(Protocol):
    """The interactions namespace (``app.interactions``) — the ``ask_user`` facade."""

    @property
    def ask_user(self) -> AskUser:
        """The bound, :class:`~tai42_contract.interactions.AskUser`-typed ``ask_user``
        callable, so an in-process plugin asks a human without importing the skeleton:
        ``await tai42_app.interactions.ask_user(question, ..., mode="async",
        expiry_at=...)``. The return shape is the ``AskUser`` contract's:
        ``mode="sync"`` returns the typed answer, ``mode="async"`` returns a
        ``SuspendedInteraction``. A facade EXPOSURE of the skeleton helper through the
        already-typed Protocol — no new ask semantics."""
        ...


@runtime_checkable
class AppStorage(Protocol):
    @overload
    def register_storage(self, cls: type[_StorageT]) -> type[_StorageT]: ...
    @overload
    def register_storage(self, cls: None = None) -> Callable[[type[Storage]], type[Storage]]: ...
    def register_storage(
        self, cls: type[Storage] | None = None
    ) -> Callable[[type[Storage]], type[Storage]] | type[Storage]: ...

    @property
    def resource_manager(self) -> Any:
        """The active resource manager (impl type; loads/renders content over storage)."""
        ...


@runtime_checkable
class AppMonitoring(Protocol):
    def register_monitoring(self, builder: Callable[..., Any] | None = None) -> Callable[..., Any]: ...

    @property
    def active(self) -> Monitoring:
        """The active monitoring backend (impl type behind the contract protocol)."""
        ...


@runtime_checkable
class AppExtensions(Protocol):
    def extension(
        self,
        f: Callable[..., Any] | None = None,
        *,
        kind: ExtensionKind,
        name: str | None = None,
        requires_body_locality: bool = False,
    ) -> Callable[..., Any]:
        """Register a tool-extension factory under ``name`` (default: the
        function's own name); usable bare or with arguments.

        ``requires_body_locality`` marks an extension whose wrapper only works
        in the process running the tool body it wraps — e.g. a proxy layer that
        routes the body's egress through a task-scoped contextvar, visible only
        where the body executes. The flag is stored as registration metadata the
        apply site reads to order a stacked combo: a locality-requiring
        extension must bind INSIDE any execution-relocating extension
        (``ExtensionKind.relocates_execution``), so its wrapper travels with the
        body to the worker. Bound outside a relocating layer, the wrapper stays
        behind in the submitting process and silently does not apply."""
        ...

    def available_extensions(self) -> list[dict[str, str]]:
        """List registered extension modules as ``[{"name", "kind"}]``."""
        ...


@runtime_checkable
class AppWebhookVerifiers(Protocol):
    def register(self, name: str, verifier: WebhookVerifier) -> None:
        """Register a :class:`WebhookVerifier` under ``name``.

        A provider plugin calls this through the ``tai42_app`` handle when its
        import-only ``webhook_verifier_modules`` entry loads. Registering a name
        already taken raises loudly — a silent overwrite could swap a topic's
        verifier out from under a live binding."""
        ...

    def get(self, name: str) -> WebhookVerifier:
        """Fetch a registered verifier by name; raise loudly on an unknown name.

        Resolution happens when a verifier is bound to a public webhook door, so
        an unknown name surfaces at bind time as a loud failure, never a
        silently-unverified door."""
        ...


@runtime_checkable
class AppChannels(Protocol):
    def register(self, name: str, channel: Channel) -> None:
        """Register a :class:`Channel` under ``name``.

        A channel plugin calls this through the ``tai42_app`` handle when its
        import-only ``channel_modules`` entry loads. Registering a name already
        taken raises loudly — a silent overwrite could swap the medium a live
        ask is delivered on."""
        ...

    def get(self, name: str) -> Channel:
        """Fetch a registered channel by name; raise loudly on an unknown name.

        Resolution happens when ``ask_user`` is called with ``channel=name``,
        BEFORE any interaction state is written, so an unknown name surfaces as
        a loud failure, never a question silently delivered nowhere."""
        ...

    def names(self) -> list[str]:
        """Every registered channel name, for the channels catalog route."""
        ...

    async def handle_inbound_answer(
        self,
        *,
        channel_id: str,
        correlation_key: str,
        answer: Any,
        store: CorrelationStore,
        bridge: InboundBridge,
    ) -> InboundAnswerOutcome:
        """Resolve one inbound guest reply against its pending ask — the ONE shared
        inbound-answer ladder every correlated channel calls instead of hand-rolling
        its own "forward → interpret 2xx/404/400 → release/bridge/keep" sequence.

        A channel computes its own opaque ``correlation_key`` for the guest's address,
        provides the ``answer`` value to forward to the door as ``{"answer": answer}``,
        its :class:`~tai42_contract.channels.CorrelationStore`, and an
        :class:`InboundBridge` of the fields a bridged turn needs. The ladder:

        * No pending ask on the key -> :attr:`InboundAnswerOutcome.NO_CORRELATION`
          with NO side effect: the CALLER bridges the reply as a normal turn (the
          ladder never bridges on a miss, exactly as each channel did by hand).
        * The door accepts (2xx) -> release the correlation, return
          :attr:`InboundAnswerOutcome.FORWARDED`.
        * The door returns 404 (the ask was withdrawn/expired/cancelled) -> release,
          bridge the reply as a fresh turn, return :attr:`InboundAnswerOutcome.BRIDGED`.
        * The door returns 400 on a LIVE ask -> read the door's ``retry_in_place``
          (default True): True keeps the correlation, notifies the guest what's
          expected, fires ONE operator event, returns :attr:`InboundAnswerOutcome.RETRY_KEPT`;
          False (a hard mismatch) releases, notifies the guest the question is closed,
          fires the event, bridges the reply, returns :attr:`InboundAnswerOutcome.BRIDGED`.
        * Anything else (401/413/5xx, or a transport fault) -> do NOT release; raise
          :class:`AnswerForwardError` so the channel's webhook redelivery re-runs the
          ladder. The answer is never silently lost.

        The callback host is pinned to the platform's configured public base before the
        forward; a stored callback whose host does not match is released and treated as
        :attr:`InboundAnswerOutcome.NO_CORRELATION`.
        """
        ...


@runtime_checkable
class AppConversations(Protocol):
    """The conversation bridge's entry surface for medium adapters: inbound messages
    via ``accept``, out-of-band delivery receipts via ``record_delivery_status``.
    Routing-row CRUD, the turn engine and the API door are not part of this facet.
    """

    async def accept(
        self,
        channel: str,
        our_identity: str,
        client_address: str,
        cap_key: str,
        text: str,
        provider_message_id: str,
        params: dict[str, str] | None = None,
    ) -> str:
        """Accept one inbound channel message, persist it, and return its
        ``message_id`` (a uuid4).

        Routed to the ``door=channel`` route matching ``(channel, our_identity)``; the
        turn runs as that route's execution key and answers back over the same channel.
        Idempotent on ``(channel, provider_message_id)`` — a redelivery returns the
        existing ``message_id`` and starts no second turn. Raises when no route matches.

        ``client_address`` is the conversation identity (its thread and transcript).
        ``cap_key`` is the party the per-address turn cap holds accountable, which the
        door composes: a provider channel passes its attested address (the two match),
        a door that mints its own visitor id passes a key the platform does NOT mint —
        its network client bucket — so the cap bounds spend the visitor cannot reset.
        Required and non-blank; a door that omits it is refused, never defaulted.

        ``params`` are optional entry parameters the door captured with the conversation
        entry, delivered verbatim to a tool target's payload under ``params``;
        ``None``/empty leaves the payload unchanged. The door validates them with
        :func:`~tai42_contract.conversations.validate_entry_params` before accept.
        """
        ...

    async def record_delivery_status(self, channel: str, provider_message_id: str, status: DeliveryReceipt) -> None:
        """Ingest a channel's out-of-band delivery receipt for an outbound message.

        ``provider_message_id`` is one of the ids a prior ``notify`` returned, resolved
        to the answer record through the outbound-id reverse index. ``FAILED`` marks the
        record failed; ``DELIVERED`` confirms a ``provisional`` record. Raises when the
        id resolves to no record.
        """
        ...


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
        """The provider the CURRENT epoch instantiated under ``name`` (an
        ``AccountsProvider`` is an ``IdentityProvider``), or ``None`` when no provider is
        active under that name — the name is not configured, or a build is mid-flight."""
        ...


@runtime_checkable
class AppConnectors(Protocol):
    def register_connector(self, descriptor: ProviderDescriptor) -> None:
        """Register a connector provider from its pure descriptor data. Called for
        every manifest ``connectors`` entry at boot/reload, and by any code holding
        the handle (a connector is pure data, so this is a plain call, not a
        decorator)."""
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
        skeleton owns the fail-close enforcement."""
        ...


@runtime_checkable
class AppHttp(Protocol):
    def middleware(self, cls: type[Any] | None = None, **options: Any) -> Callable[..., Any]: ...

    def custom_route(
        self,
        path: str,
        methods: list[str],
        name: str | None = None,
        include_in_schema: bool = True,
        *,
        summary: str,
        tags: list[str],
        response_model: type[BaseModel] | None,
        request_model: type[BaseModel] | None = None,
        query_model: type[BaseModel] | None = None,
        authed: bool | None = None,
        destructive: bool = False,
        action: RouteAction | None = None,
        declared: DeclaredRouteMetadata | None = None,
    ) -> Callable[[Callable[[Request], Awaitable[Response]]], Callable[[Request], Awaitable[Response]]]:
        """Register a Starlette handler AND its self-describing route metadata.

        The metadata is the single source of truth the OpenAPI 3.1 emitter and its
        coverage gate consume, so every registration MUST describe itself:

        * ``summary`` — a one-line operation summary (required, non-empty).
        * ``tags`` — at least one OpenAPI tag grouping the route (required).
        * ``response_model`` — the pydantic model wrapped in the ``{"data": ...}``
          success envelope, or ``None`` as the explicit opaque marker for a route
          with no structured response. Required — there is no default, so a route
          cannot silently omit it.
        * ``request_model`` — the pydantic model of the request body; required for
          any route that reads a body, omitted (``None``) otherwise.
        * ``query_model`` — the pydantic model whose fields are published as ``in:
          query`` parameters for ANY method, additive to ``request_model``. A read
          door's ``request_model`` already emits as query, so this is the only way a
          WRITE-method door documents the query it reads at the edge; ``None`` (the
          default) publishes no query model.
        * ``authed`` — whether the route requires the api key; emitted as the
          ``security`` requirement. ``None`` (the default) defers to the runtime:
          a core route resolves to ``True``, a declared plugin route to the
          negation of its ``tai-plugin.yml`` ``public`` flag. Passing an explicit
          bool from a declared plugin route module is a registration error — the
          declaration is the single source of that decision.
        * ``destructive`` — whether the route mutates in a way flagged as
          destructive (default ``False``); emitted as ``x-destructive``.
        * ``action`` — the route's authorization character (a :data:`RouteAction`);
          ``None`` (the default) lets the surface derive the grantable class from
          the HTTP methods.
        * ``declared`` — the route's behavioral OpenAPI properties (a
          :class:`DeclaredRouteMetadata`): its ``reload_gated`` / ``reads_body`` /
          error statuses / success status. A route in the ``/api/*`` spec surface
          declares these (the operations adapter passes its operation's metadata, a
          native handler passes its own); ``None`` (the default) records trivial
          defaults for a route outside the spec surface, whose behavioral metadata
          is never emitted.

        The handler's narrative docstring becomes the operation description, and
        the reload-gate ``503`` response is derived from the handler body."""
        ...

    def mount_base(self) -> str:
        """The resolved absolute mount base of the declared plugin route module
        importing now — ``/api/`` + the item's mount base, no trailing slash
        (e.g. ``/api/channels/telegram``).

        Callable ONLY while a declared plugin route module imports — the mount
        binding is present then. A module captures this value at import for later
        use (a startup hook building an external webhook URL, a login descriptor's
        self-referential path) so a remapped base is followed instead of a
        hardcoded default. Called with no binding present — a core or
        operator-authored module — it raises: those modules own no declared mount."""
        ...


@runtime_checkable
class AppClients(Protocol):
    def client_ctx[ClientT](
        self,
        client_cls: type[BaseClient[ClientT]],
        settings: Any = None,
        *,
        fresh: bool = False,
        **kwargs: Any,
    ) -> AbstractAsyncContextManager[ClientT]:
        """Async context manager yielding a connected client, pooled per loop +
        connection params (or one-shot when ``fresh=True``). ``settings`` is a kit
        ``ClientSettings`` whose ``client_kwargs()`` supplies the connection key."""
        ...

    async def shutdown_clients(self) -> None:
        """Close every live client pool for the running loop."""
        ...


@runtime_checkable
class AppLifecycle(Protocol):
    def on_startup(self, func: Callable[[], Any]) -> Callable[[], Any]: ...

    def on_shutdown(self, func: Callable[[], Any]) -> Callable[[], Any]: ...

    def on_reload(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register a handler re-run after every in-place re-init (``reload_config``) —
        e.g. dynamic tool loaders that ``on_startup`` ran once."""
        ...

    def on_post_swap(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register an establisher for a loop-affine background loop (a periodic poll or
        sweep). Run on the real serving loop at boot and after every epoch swap — never
        on the throwaway build-thread loop the per-epoch handlers run on — so the loop
        it spawns attaches to the serving loop and retires with its generation."""
        ...

    def on_fleet_op_applied(self, func: Callable[[str], Any]) -> Callable[[str], Any]:
        """Register a handler fired after any worker-bus op applies in this
        process, and after the reconnect self-resync reload.

        Unlike its zero-arg siblings, the handler takes ONE argument — the op
        name — so it can act on some ops and skip others (a query op carries no
        state change to react to)."""
        ...

    async def wait_until_ready(self) -> None:
        """Block until this process's first boot self-resync has completed — the
        point at which the tool registry is fully built and stable for the run.

        A backend runtime that forks a child (or otherwise consumes queued work)
        per job awaits this before its work loop accepts anything: the boot
        self-resync rebuilds the tool registry non-atomically, so a worker that
        dequeued and forked mid-rebuild would run the job against a half-built
        registry. The latch is one-way — it resolves once and stays resolved, so
        a later bus reconnect (which re-runs the self-resync while the app is
        already live) never un-readies the process."""
        ...


@runtime_checkable
class AppAdmin(Protocol):
    def reload_mcp(self, title: str) -> dict[str, Any]: ...

    def deregister_mcp(self, title: str) -> dict[str, Any]: ...

    def reload_config(self) -> dict[str, Any]: ...

    def tool_reloader(self, kind: str) -> Callable[..., Any]:
        """Register an async ``(action, name) -> dict`` reloader for ``kind``."""
        ...

    async def run_tool_reload(self, kind: str, action: str, name: str) -> dict[str, Any]: ...

    def reload_failed_mcps(self) -> list[dict[str, Any]]: ...

    def list_failed_mcps(self) -> list[dict[str, Any]]: ...

    def live_mcp_status(self) -> dict[str, Any]:
        """Snapshot the in-process MCP-binding state."""
        ...

    @property
    def live_manifest(self) -> dict[str, Any]: ...


@runtime_checkable
class AppConfig(Protocol):
    @property
    def config_manager(self) -> ConfigManager: ...


@runtime_checkable
class AppBackup(Protocol):
    """Registry for named backup sections and the run of one section's
    export/import.

    A plugin (or the host itself, the first consumer) registers a section under
    ``name`` by supplying an ``exporter()`` that returns a JSON-safe payload and
    an ``importer(payload)`` that applies it and returns a section report.
    ``sections()`` lists the registered sections for the UI. ``export_section``
    / ``import_section`` run one section's exporter/importer by name and raise
    loudly on an unknown name — never a silent no-op.
    """

    def register_section(
        self, name: str, exporter: Callable[[], Any], importer: Callable[[Any], Any], *, secret: bool = False
    ) -> None: ...

    def sections(self) -> list[BackupSectionInfo]: ...

    def export_section(self, name: str) -> Any: ...

    def import_section(self, name: str, payload: Any) -> Any: ...


@runtime_checkable
class AppSubApp(Protocol):
    @property
    def mcp_sub_app_router(self) -> SubMcpAppRouter: ...


@runtime_checkable
class AppVersioning(Protocol):
    """The generic versioned-document store namespace (``app.versioning``).

    This is the platform persistence primitive — append-only versions + an active
    pointer + rollback over an opaque JSONB body, discriminated by ``kind``. Direct
    consumers (e.g. AC policies under ``kind="ac_policy"``) reach it here; presets
    reach it through :class:`AppPresets`, never as a new kind.
    """

    @property
    def store(self) -> VersionedStore: ...


@runtime_checkable
class AppPresets(Protocol):
    """The presets namespace (``app.presets``) — the typed view + the bind kernel.

    ``store`` is the :class:`~tai42_contract.presets.PresetStore`-typed view over
    ``app.versioning.store`` (``kind="preset"``). ``bind`` is the kernel BOTH tiers
    (ephemeral and versioned) build their live tool through, so its typed-schema
    behavior reaches both.
    """

    async def bind(
        self,
        base_tool: str,
        fixed_kwargs: dict[str, Any],
        *,
        name: str,
        description: str = "",
        tags: list[str] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> Tool:
        """Return a FastMCP tool transform of ``base_tool`` as a new named tool.

        Built in ONE ``Tool.from_tool`` call: each ``fixed_kwargs`` key is baked as
        a HIDDEN, FIXED constant (removed from the exposed schema; a caller that
        passes it is rejected — it cannot be overridden at runtime) while the
        REMAINING arguments keep the base tool's real typed schema (names, types,
        descriptions). ``tags`` sets the transformed tool's native tags. An
        ``output_schema`` (an object JSON Schema) is baked into an agent base's
        ``response_format`` (forcing structured output) or advertised + validated on
        a plain tool base. ``bind`` is async because it must resolve the base
        ``Tool`` object (via ``app.tools.get_tool(base_tool)``) to feed the
        transform."""
        ...

    def register_write_validator(self, base_tool: str, validator: PresetWriteValidator) -> None:
        """Register the write validator for ``base_tool`` (one per base tool;
        duplicate registration raises).

        A base-tool plugin calls this through the ``tai42_app`` handle when its tool
        module loads. The validator runs on every write that persists a body —
        create / save-version / rollback — and in the dry-run ``validate`` verdict,
        so a body its base tool cannot accept is a loud 400 that never persists. A
        base tool with no registered validator gets no extra check."""
        ...

    def register_input_schema_support(self, base_tool: str, support: PresetInputSchemaSupport) -> None:
        """Declare that ``base_tool`` ACCEPTS a per-preset input schema (one per base
        tool; duplicate registration raises).

        A base-tool plugin calls this through the ``tai42_app`` handle when its tool
        module loads. A preset that sets an ``input_schema`` over a base tool with no
        registered support is a loud authoring error at the shared preset-authoring
        chokepoint, never a silently-ignored schema."""
        ...

    def input_schema_support(self, base_tool: str) -> PresetInputSchemaSupport | None:
        """The input-schema support ``base_tool`` declared, or ``None`` if it declared
        none (its typed schema is fixed)."""
        ...

    def register_registration_tier(self, base_tool: str, tier: RouteAction) -> None:
        """Declare the authz character required to AUTHOR (create/save/rollback/rename)
        a preset over ``base_tool`` (one per base tool; duplicate registration raises).

        Default authoring is the presets' own ``write`` action; a base tool with no
        declaration keeps that default. A base tool declaring ``fenced`` requires the
        admin fence to author a preset over it. The shared preset-authoring chokepoint
        reads this and enforces it BEFORE any store write on every authoring door."""
        ...

    def registration_tier(self, base_tool: str) -> RouteAction | None:
        """The authoring authz tier ``base_tool`` declared, or ``None`` if it declared
        none (authoring keeps the presets' default ``write`` action)."""
        ...

    @property
    def store(self) -> PresetStore: ...
