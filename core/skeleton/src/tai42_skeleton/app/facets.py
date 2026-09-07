"""Facet adapters mapping the concrete app across the facade's
``tai42_contract.app`` sub-protocols.

Each facet is a thin view bound to the owning :class:`~tai42_skeleton.app.server.TaiMCP`;
it forwards to the feature's impl collaborator (``ToolBinding``, ``AgentBinding``,
``BackendHolder``, the extension/monitoring registries, ``HttpSurface``, ...) so
the concrete app satisfies ``tai42_contract.app.TaiApp`` (every member partitioned
onto exactly one namespace). The facets are the app's SOLE feature/contract
surface; the concrete server additionally exposes a launch surface outside the
facade. The facets carry no state of their own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from tai42_contract.backup import BackupSectionInfo
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.connectors.store import ConnectorTokenStore
from tai42_contract.extensions import ExtensionKind
from tai42_contract.manifest import ExtensionElement
from tai42_contract.presets import CARRY_FORWARD

from tai42_skeleton.extensions.registry import extension_name

if TYPE_CHECKING:
    from fastmcp.tools import Tool
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel
    from starlette.requests import Request
    from starlette.responses import Response
    from tai42_contract.access_control.identity import IdentityProvider
    from tai42_contract.agent import Agent
    from tai42_contract.app import DeclaredRouteMetadata
    from tai42_contract.backend import Backend
    from tai42_contract.config import ConfigManager
    from tai42_contract.connectors.models import ResolvedConnectionAuth
    from tai42_contract.interactions import AskUser
    from tai42_contract.monitoring import Monitoring
    from tai42_contract.presets import (
        CarryForward,
        PresetBody,
        PresetInputSchemaSupport,
        PresetSeed,
        PresetStore,
        PresetWriteValidator,
    )
    from tai42_contract.sandbox import Sandbox, SandboxPolicy
    from tai42_contract.states import (
        ApplyResult,
        ConsumerLister,
        ConsumerRow,
        MountBody,
        MountValidator,
        RecordView,
        StateContext,
        StateDeclaration,
        StateModuleDocument,
        StateSubject,
        WriteOrigin,
        WritesPage,
    )
    from tai42_contract.storage import Storage
    from tai42_contract.sub_mcp import SubMcpAppRouter
    from tai42_contract.tool_meta import ToolMetaStore
    from tai42_contract.tools import ToolRefsExtractor, ToolRenameReferee
    from tai42_contract.versioning import VersionedStore
    from tai42_contract.webhooks import WebhookVerifier

    from tai42_skeleton.app.route_registry import RouteAction
    from tai42_skeleton.app.server import TaiMCP
    from tai42_skeleton.manifest import Manifest as ManifestImpl
    from tai42_skeleton.template import ResourceManager

    _AgentT = TypeVar("_AgentT", bound=Agent)


class _Facet:
    """Common base: binds the facet to its owning app."""

    __slots__ = ("_app",)

    def __init__(self, app: TaiMCP) -> None:
        self._app = app


class ToolsFacet(_Facet):
    """``app.tools`` — tool/toolkit registration + lookup (``AppTools``)."""

    def tool(self, *args, force: bool = False, **kwargs) -> Callable[..., Any]:
        return self._app._tool_binding.tool(*args, force=force, **kwargs)

    def toolkit(self, *args, **kwargs):
        return self._app._tool_binding.toolkit(*args, **kwargs)

    def tool_title(self, func) -> str:
        return self._app._tool_binding.tool_title(func)

    async def get_tool(self, key: str) -> Tool:
        return await self._app._tool_binding.get_tool(key)

    async def get_tools(self) -> dict[str, Tool]:
        return await self._app._tool_binding.get_tools()

    async def get_client_tools(self, names: list[str] | None = None) -> list[StructuredTool]:
        return await self._app._tool_binding.get_client_tools(names)

    async def run_tool(self, key: str, arguments: dict[str, Any], *, offload_sync: bool = False) -> Any:
        return await self._app._tool_binding.run_tool(key, arguments, offload_sync=offload_sync)

    def remove_tool(self, name: str) -> None:
        return self._app._tool_binding.remove_tool(name)

    def register_tool_info(self, name: str, combos: Sequence[Sequence[ExtensionElement]] | None = None):
        return self._app._tool_binding.register_tool_info(name, combos)

    def unregister_tool_info(self, name: str):
        return self._app._tool_binding.unregister_tool_info(name)

    def unregister_tool_base(self, tool_name: str) -> list[str]:
        return self._app._tool_binding.unregister_tool_base(tool_name)

    def tool_refs_extractor(self, name: str) -> ToolRefsExtractor | None:
        """The declared tool-references extractor a base tool registered under
        ``name``, or ``None`` when it declared none."""
        return self._app._tool_binding.tool_refs_extractor(name)

    def base_of(self, name: str) -> str:
        """The base tool ``name`` was produced from (``name`` itself for a base or
        an unbound name; the origin base for an extension branch)."""
        return self._app._tool_binding.base_of(name)

    def is_branch(self, name: str) -> bool:
        """Whether ``name`` is an extension branch tool rather than a base."""
        return self._app._tool_binding.is_branch(name)

    def mcp_bound_names(self, title: str) -> frozenset[str]:
        """A read-only snapshot of the tool names the MCP server ``title`` currently
        binds (empty for an unknown title)."""
        return self._app._tool_binding.mcp_bound_names(title)

    def register_rename_referee(self, provider: ToolRenameReferee) -> None:
        return self._app._rename_referee_registry.register(provider)

    def rename_referees(self) -> list[ToolRenameReferee]:
        """Every registered rename referee (plugin providers + platform-internal
        wiring). Skeleton-only — the rename gate and the referees preview door consult
        it, so it is not on the ``AppTools`` protocol (the register-only seam), the same
        precedent :meth:`PresetsFacet.write_validator` sets."""
        return self._app._rename_referee_registry.all()


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


class InteractionsFacet(_Facet):
    """``app.interactions`` — the ``ask_user`` facade (``AppInteractions``)."""

    @property
    def ask_user(self) -> AskUser:
        """The bound, ``AskUser``-typed ``ask_user`` callable, so an in-process plugin asks
        a human without importing the skeleton. A facade EXPOSURE of the existing helper —
        its rich signature and return contract are forwarded verbatim, no new ask
        semantics."""
        from tai42_skeleton.interactions.helper import ask_user

        return ask_user


class AccountsFacet(_Facet):
    """``app.accounts`` — read access to the current epoch's live provider instances
    (``AppAccounts``)."""

    def active_provider(self, name: str) -> IdentityProvider | None:
        # Resolve the CURRENT (live) epoch's provider, never the generation under
        # construction — a failed build's provider instances must never bind into a
        # surviving epoch's memoized verifier (the zero-mutation invariant). See
        # ``TaiMCP._live_serving_core``.
        return self._app._live_serving_core.active_auth_providers.get(name)


class HttpFacet(_Facet):
    """``app.http`` — middleware + custom-route registration (``AppHttp``)."""

    def middleware(self, cls: type | None = None, **options: Any) -> Callable[..., Any]:
        return self._app._http_surface.middleware(cls, **options)

    def mount_base(self) -> str:
        return self._app._http_surface.mount_base()

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
        return self._app._http_surface.custom_route(
            path,
            methods,
            name,
            include_in_schema,
            summary=summary,
            tags=tags,
            response_model=response_model,
            request_model=request_model,
            query_model=query_model,
            authed=authed,
            destructive=destructive,
            action=action,
            declared=declared,
        )


class LifecycleFacet(_Facet):
    """``app.lifecycle`` — startup/shutdown/reload handler registration
    (``AppLifecycle``)."""

    def on_startup(self, func: Callable[[], Any]) -> Callable[[], Any]:
        return self._app._on_startup(func)

    def on_shutdown(self, func: Callable[[], Any]) -> Callable[[], Any]:
        return self._app._on_shutdown(func)

    def on_reload(self, func: Callable[[], Any]) -> Callable[[], Any]:
        return self._app._on_reload(func)

    def on_post_swap(self, func: Callable[[], Any]) -> Callable[[], Any]:
        """Register an establisher for a loop-affine background loop, run on the serving
        loop at boot and after every epoch swap — never on the throwaway build-thread
        loop the per-epoch handlers run on."""
        return self._app._on_post_swap(func)

    def on_fleet_op_applied(self, func: Callable[[str], Any]) -> Callable[[str], Any]:
        return self._app._on_fleet_op_applied(func)

    def reload_registries(self, manifest: ManifestImpl) -> dict[str, Any]:
        """Re-initialise the registries from ``manifest`` and run the per-epoch
        handler list once — the rebuild step the epoch build+swap primitive calls."""
        return self._app._reload_registries(manifest)

    async def wait_until_ready(self) -> None:
        await self._app._wait_until_ready()

    def read_boot_manifest(self) -> ManifestImpl:
        """Bridge the persisted env store into ``os.environ``, read + validate the boot
        manifest under it, and name any still-dangling ``!ENV`` marker — the one
        cold-boot manifest read every serving/backend entrypoint crosses."""
        return self._app._read_boot_manifest()


class AdminFacet(_Facet):
    """``app.admin`` — runtime management surface (``AppAdmin``)."""

    def reload_mcp(self, title: str) -> dict[str, Any]:
        return self._app._reload_mcp(title)

    def deregister_mcp(self, title: str) -> dict[str, Any]:
        return self._app._deregister_mcp(title)

    def reload_config(self) -> dict[str, Any]:
        return self._app._reload_config()

    def tool_reloader(self, kind: str) -> Callable[..., Any]:
        return self._app._tool_reloader(kind)

    async def run_tool_reload(self, kind: str, action: str, name: str) -> dict[str, Any]:
        return await self._app._run_tool_reload(kind, action, name)

    def reload_failed_mcps(self) -> list[dict[str, Any]]:
        return self._app._reload_failed_mcps()

    def list_failed_mcps(self) -> list[dict[str, Any]]:
        return self._app._list_failed_mcps()

    def live_mcp_status(self) -> dict[str, Any]:
        return self._app._live_mcp_status()

    @property
    def live_manifest(self) -> dict[str, Any]:
        return self._app._live_manifest

    @property
    def live_manifest_typed(self) -> ManifestImpl:
        """The live in-process manifest as the skeleton ``Manifest`` (its resolved
        selection maps + predicates), raising if the app is not started — the typed
        companion to :attr:`live_manifest` (the emitted, model-dumped dict).

        This is the shared live object, not a copy (the predicates and resolved maps
        are the point; a copy would lose them). Read-only: callers must not mutate it —
        an edit belongs on a fresh ``config_manager.read_manifest()`` dict, never here.
        """
        return self._app._require_live_manifest()


class ConfigFacet(_Facet):
    """``app.config`` — the active config manager (``AppConfig``)."""

    @property
    def config_manager(self) -> ConfigManager:
        return self._app._config_manager


class SubAppFacet(_Facet):
    """``app.sub_app`` — the live sub-MCP app router (``AppSubApp``)."""

    @property
    def mcp_sub_app_router(self) -> SubMcpAppRouter:
        return self._app._mcp_sub_app_router


class VersioningFacet(_Facet):
    """``app.versioning`` — the generic versioned-document store (``AppVersioning``)."""

    @property
    def store(self) -> VersionedStore:
        return self._app._versioned_store


class PresetsFacet(_Facet):
    """``app.presets`` — the preset bind kernel + the typed store view (``AppPresets``)."""

    async def bind(
        self,
        base_tool: str,
        fixed_kwargs: dict[str, Any],
        *,
        name: str,
        description: str = "",
        output_schema: dict[str, Any] | None = None,
        input_schema: dict[str, Any] | None = None,
    ) -> Tool:
        return await self._app._preset_bind(
            base_tool,
            fixed_kwargs,
            name=name,
            description=description,
            output_schema=output_schema,
            input_schema=input_schema,
        )

    async def create(
        self,
        name: str,
        base_tool: str,
        description: str,
        fixed_kwargs: dict[str, Any],
        *,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        extensions: list[list[ExtensionElement]] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a preset in-process, returning the record view the HTTP create door
        returns (shared response builder — the two doors cannot drift). Runs the
        identical content path: the ordered name pre-checks, combo/schema/bind
        validation, input-schema support, the base tool's write validator, store write
        THEN register, one ``list_changed``, and the rebind fan-out.

        The caller-authorization tier fence is OFF: the registration-tier fence belongs to
        the HTTP doors, where it authorizes a request principal. An in-process caller
        authorizes its own callers and passes only the base tools it is entitled to author,
        so there is no request principal to fence here — ``enforce_tier`` is ``False`` while
        every content check stays on."""
        from tai42_skeleton.operations.presets import _create_preset_core, _create_response

        combos = extensions or []
        record, report = await _create_preset_core(
            name,
            base_tool,
            description,
            fixed_kwargs,
            combos,
            output_schema,
            input_schema,
            tags=tags,
            enforce_tier=False,
        )
        return await _create_response(
            name,
            base_tool,
            description,
            combos,
            output_schema,
            input_schema,
            active_version=record.active_version,
            report=report,
        )

    async def save_version(
        self,
        name: str,
        *,
        fixed_kwargs: dict[str, Any] | None = None,
        input_schema: dict[str, Any] | CarryForward | None = CARRY_FORWARD,
        output_schema: dict[str, Any] | None = None,
        output_schema_provided: bool = False,
        description: str | None = None,
        extensions: list[list[ExtensionElement]] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Save a new preset version in-process, returning the version row the HTTP save
        door returns (shared response builder — the two doors cannot drift). Omitted
        fields carry the active value forward (``input_schema`` via the ``CARRY_FORWARD``
        sentinel; ``output_schema`` only when ``output_schema_provided`` is ``True``).

        The tier fence is OFF for the reason :meth:`create` states — the registration-tier
        fence authorizes a request principal at the HTTP doors, and an in-process caller
        authorizes its own callers and passes only the base tools it may author — while
        every content check stays on."""
        from tai42_skeleton.operations.presets import _save_version_core, _save_version_response

        row, report = await _save_version_core(
            name,
            fixed_kwargs=fixed_kwargs,
            extensions=extensions,
            output_schema=output_schema,
            output_schema_provided=output_schema_provided,
            description=description,
            input_schema=input_schema,
            tags=tags,
            enforce_tier=False,
        )
        return _save_version_response(row, report)

    def register_write_validator(self, base_tool: str, validator: PresetWriteValidator) -> None:
        return self._app._write_validator_registry.register(base_tool, validator)

    def register_input_schema_support(self, base_tool: str, support: PresetInputSchemaSupport) -> None:
        return self._app._input_schema_support_registry.register(base_tool, support)

    def input_schema_support(self, base_tool: str) -> PresetInputSchemaSupport | None:
        return self._app._input_schema_support_registry.get(base_tool)

    def register_registration_tier(self, base_tool: str, tier: RouteAction) -> None:
        return self._app._registration_tier_registry.register(base_tool, tier)

    def registration_tier(self, base_tool: str) -> RouteAction | None:
        return self._app._registration_tier_registry.get(base_tool)

    def register_seed(self, seed: PresetSeed) -> None:
        return self._app._seed_registry.register(seed)

    def register_retired_seed(self, name: str) -> None:
        return self._app._seed_registry.register_retired(name)

    def seeds(self) -> list[PresetSeed]:
        """Every declared preset seed. Skeleton-only — the startup/reload seed applier
        consults it, so it is not on the ``AppPresets`` protocol (the register-only
        seam), the same precedent :meth:`write_validator` sets."""
        return self._app._seed_registry.all()

    def retired_seeds(self) -> list[str]:
        """Every declared retired seed name. Skeleton-only — the startup/reload seed
        applier consults it, so it is not on the ``AppPresets`` protocol (the
        register-only seam), the same precedent :meth:`seeds` sets."""
        return self._app._seed_registry.retired()

    def write_validator(self, base_tool: str) -> PresetWriteValidator | None:
        """The registered write validator for ``base_tool``, or ``None`` when none
        is registered. Skeleton-only — the preset write path consults it, so it is
        not on the ``AppPresets`` protocol (the precedent :meth:`list_active_bodies`
        sets)."""
        return self._app._write_validator_registry.get(base_tool)

    @property
    def store(self) -> PresetStore:
        return self._app._preset_store

    async def list_active_bodies(self) -> dict[str, PresetBody]:
        """Every store-backed preset's active body, keyed by name — one batched
        JOIN read (replaces a per-record ``get_active_body`` round-trip on the list
        route + rehydrate). Reached through the concrete ``_versioned_store`` so the
        concrete-only ``list_active_bodies`` resolves."""
        from tai42_contract.presets import PresetBody

        raw = await self._app._versioned_store.list_active_bodies("preset")
        return {name: PresetBody.model_validate(body) for name, body in raw.items()}

    async def list_active_versioned_bodies(self) -> dict[str, tuple[int, PresetBody]]:
        """Every preset's ``(active_version, active_body)``, keyed by name — one batched
        JOIN read that captures the version pointer and its body TOGETHER (never a
        skewed second read). The version-aware sibling of :meth:`list_active_bodies`,
        used by the rehydration path so the engine retains each preset's version beside
        its body. Reached through the concrete ``_versioned_store``."""
        from tai42_contract.presets import PresetBody

        raw = await self._app._versioned_store.list_active_versioned_bodies("preset")
        return {name: (version, PresetBody.model_validate(body)) for name, (version, body) in raw.items()}

    async def get_active_versioned_body(self, name: str) -> tuple[int, PresetBody]:
        """One preset's ``(active_version, active_body)``, read TOGETHER in one JOIN.

        The version-aware sibling of ``store.get_active_body`` used by the edit-reload
        path so the freshly-active version is retained beside the body it re-binds.
        Maps the generic store's ``DocumentNotFoundError`` to
        :class:`~tai42_contract.presets.errors.PresetNotFoundError`, mirroring
        :class:`~tai42_skeleton.presets.store.PresetStoreView`. Reached through the
        concrete ``_versioned_store``."""
        from tai42_contract.presets import PresetBody
        from tai42_contract.presets.errors import PresetNotFoundError
        from tai42_contract.versioning.errors import DocumentNotFoundError

        try:
            version, body = await self._app._versioned_store.get_active_version_and_body("preset", name)
        except DocumentNotFoundError as exc:
            raise PresetNotFoundError(name) from exc
        return version, PresetBody.model_validate(body)

    async def set_version_tags(self, name: str, version: int, tags: list[str]) -> None:
        """Replace the per-version ``tags`` annotation of one preset version.

        Tags are labels on an immutable version body, not content — this edits the
        annotation only and never rebinds the live tool. Reached through the
        concrete ``_versioned_store`` (the ``set_version_tags`` UPDATE is a
        concrete-store member, not on the ``VersionedStore`` protocol), the same
        precedent as :meth:`list_active_bodies`. Raises
        :class:`~tai42_contract.versioning.errors.DocumentVersionNotFoundError` for an
        unknown preset or version."""
        await self._app._versioned_store.set_version_tags("preset", name, version, tags)


class ToolMetaFacet(_Facet):
    """``app.tool_meta`` — the tool-metadata overlay (folders + per-tool rows), the
    ``tai42_contract.app.AppToolMeta`` namespace: the ``store`` view plus the
    in-process ``patch`` edit seam."""

    @property
    def store(self) -> ToolMetaStore:
        return self._app._tool_meta_store

    async def patch(
        self,
        tool_name: str,
        *,
        tags: list[str] | None = None,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """Patch a tool's overlay row in-process, returning the record the HTTP PATCH
        door returns (the same operation, so validation is identical: an unknown
        ``folder_id`` is the same loud error). Only the arguments given are written:
        ``folder_id`` places the tool, and a ``tags`` list REPLACES the whole tag set
        (the door's merge-patch semantic — array values are set replacements, never
        merges). ``tags=None`` leaves the tag set untouched; ``folder_id=None`` leaves
        the placement untouched."""
        from tai42_skeleton.operations.tool_meta import upsert_tool_meta

        patch: dict[str, Any] = {}
        if tags is not None:
            patch["tags"] = tags
        if folder_id is not None:
            patch["folder_id"] = folder_id
        return await upsert_tool_meta(tool_name, patch)


class BackupFacet(_Facet):
    """``app.backup`` — the named backup-section registry (``AppBackup``)."""

    def register_section(
        self, name: str, exporter: Callable[[], Any], importer: Callable[[Any], Any], *, secret: bool = False
    ) -> None:
        return self._app._backup_registry.register_section(name, exporter, importer, secret=secret)

    def sections(self) -> list[BackupSectionInfo]:
        return self._app._backup_registry.sections()

    def export_section(self, name: str) -> Any:
        return self._app._backup_registry.export_section(name)

    def import_section(self, name: str, payload: Any) -> Any:
        return self._app._backup_registry.import_section(name, payload)


class StatesFacet(_Facet):
    """``app.states`` — the subject-keyed state store namespace (``AppStates``): the
    door-agnostic record substrate every door and tool reads and writes a subject's
    document through, plus the module/mount lifecycle and the consumer/seed/mount-validator
    seams. Forwards to the app's shared :class:`~tai42_skeleton.states.service.StatesService`
    and its registries; the write chokepoint completes provenance and the gate refuses 501
    while the store is unbound."""

    # -- declarations --
    async def list_declarations(self) -> list[StateDeclaration]:
        return await self._app._states_service.list_declarations()

    async def get_declaration(self, name: str) -> StateDeclaration | None:
        return await self._app._states_service.get_declaration(name)

    async def served_declaration(self, name: str) -> dict[str, Any]:
        """The composed declaration read the ``GET /api/states/{name}`` route serves: base
        ``schema``, composed ``effective_schema``, ``subject_kinds``, ``default_subject_kind``,
        the state's ``mounts`` and computed ``regimes``. Skeleton-only (the HTTP composed
        view), so it is off the ``AppStates`` protocol — a consumer reads the typed
        :meth:`get_declaration` + :meth:`list_mounts` — the ``target_validator`` precedent."""
        return await self._app._states_service.served_declaration(name)

    async def put_declaration(self, decl: StateDeclaration) -> StateDeclaration:
        return await self._app._states_service.put_declaration(decl)

    async def delete_declaration(self, name: str) -> None:
        return await self._app._states_service.delete_declaration(name)

    async def stats(self, name: str) -> dict[str, Any]:
        return await self._app._states_service.stats(name)

    async def migrate(
        self,
        name: str,
        new_schema: dict[str, Any],
        *,
        origin: WriteOrigin,
        transform_expr: str | None = None,
        confirm_drop: bool = False,
        resolutions: list[dict[str, Any]] | None = None,
    ) -> None:
        return await self._app._states_service.migrate(
            name,
            new_schema,
            origin=origin,
            transform_expr=transform_expr,
            confirm_drop=confirm_drop,
            resolutions=resolutions,
        )

    async def preview_migrate(self, name: str, new_schema: dict[str, Any]) -> dict[str, Any]:
        return await self._app._states_service.preview_migrate(name, new_schema)

    # -- modules --
    async def list_modules(self) -> list[StateModuleDocument]:
        return await self._app._states_service.list_modules()

    async def list_modules_catalog(self) -> list[dict[str, Any]]:
        """The module-catalog projection the ``GET /api/state-modules`` list route serves:
        each stored document plus ``mounted_on`` (the number of states it is mounted on)
        and ``shipped_default`` (whether it is an unedited shipped default). Skeleton-only
        (the HTTP catalog view), so it is off the ``AppStates`` protocol — a consumer reads
        the typed :meth:`list_modules` — the ``served_declaration`` precedent."""
        return await self._app._states_service.list_modules_catalog()

    async def get_module(self, name: str) -> StateModuleDocument | None:
        return await self._app._states_service.get_module(name)

    async def put_module(self, doc: StateModuleDocument, *, replace: bool) -> StateModuleDocument:
        return await self._app._states_service.put_module(doc, replace=replace)

    async def delete_module(self, name: str) -> None:
        return await self._app._states_service.delete_module(name)

    # -- mounts --
    async def list_mounts(self, state: str | None = None, *, module: str | None = None) -> list[dict[str, Any]]:
        return await self._app._states_service.list_mounts(state, module=module)

    async def mount(self, state: str, module: str, body: MountBody) -> None:
        return await self._app._states_service.mount(state, module, body)

    async def update_mount_declarations(self, state: str, module: str, declarations: dict[str, Any]) -> None:
        return await self._app._states_service.update_mount_declarations(state, module, declarations)

    async def unmount(self, state: str, module: str) -> None:
        return await self._app._states_service.unmount(state, module)

    # -- bulk import --
    async def import_aliases(self, state: str, rows: Sequence[dict[str, Any]], *, origin: WriteOrigin) -> None:
        return await self._app._states_service.import_aliases(state, rows, origin=origin)

    async def import_applied_ops(self, rows: Sequence[dict[str, Any]]) -> None:
        return await self._app._states_service.import_applied_ops(rows)

    async def import_records(self, state: str, rows: Sequence[dict[str, Any]], *, origin: WriteOrigin) -> None:
        return await self._app._states_service.import_records(state, rows, origin=origin)

    # -- records --
    async def read(self, state: str, subject: StateSubject) -> RecordView | None:
        return await self._app._states_service.read(state, subject)

    async def replace(
        self, state: str, subject: StateSubject, data: dict[str, Any], *, origin: WriteOrigin
    ) -> RecordView:
        return await self._app._states_service.replace(state, subject, data, origin=origin)

    async def merge(
        self, state: str, subject: StateSubject, patch: dict[str, Any], *, origin: WriteOrigin
    ) -> RecordView:
        return await self._app._states_service.merge(state, subject, patch, origin=origin)

    async def apply(
        self,
        state: str,
        subject: StateSubject,
        ops: list[dict[str, Any]],
        *,
        op_id: str | None,
        origin: WriteOrigin,
    ) -> ApplyResult:
        return await self._app._states_service.apply(state, subject, ops, op_id=op_id, origin=origin)

    async def erase(self, state: str, subject: StateSubject, *, origin: WriteOrigin) -> None:
        return await self._app._states_service.erase(state, subject, origin=origin)

    async def fold(
        self, state: str, subject: StateSubject, into: StateSubject, mode: str, *, origin: WriteOrigin
    ) -> dict[str, Any]:
        return await self._app._states_service.fold(state, subject, into, mode, origin=origin)

    async def list_subjects(
        self, state: str, *, kind: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        return await self._app._states_service.list_subjects(state, kind=kind, limit=limit, cursor=cursor)

    async def search(
        self, state: str, filters: dict[str, Any], *, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        return await self._app._states_service.search(state, filters, limit=limit, cursor=cursor)

    async def writes(
        self, state: str, subject: StateSubject, *, limit: int | None = None, cursor: str | None = None
    ) -> WritesPage:
        return await self._app._states_service.writes(state, subject, limit=limit, cursor=cursor)

    async def prune_expired(self) -> dict[str, int]:
        return await self._app._states_service.prune_expired()

    # -- context --
    def context(self) -> StateContext | None:
        return self._app._states_service.context()

    # -- consumers --
    def register_consumer_lister(self, kind: str, lister: ConsumerLister) -> None:
        return self._app._states_service.register_consumer_lister(kind, lister)

    async def consumers(self, state: str) -> list[ConsumerRow]:
        return await self._app._states_service.consumers(state)

    # -- seeds --
    def register_module_seed(self, doc: StateModuleDocument) -> None:
        return self._app._states_service.register_module_seed(doc)

    def register_retired_module_name(self, name: str) -> None:
        return self._app._states_service.register_retired_module_name(name)

    # -- mount validation --
    def register_mount_validator(self, validator: MountValidator) -> None:
        return self._app._states_service.register_mount_validator(validator)
