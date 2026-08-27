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
        PresetBody,
        PresetInputSchemaSupport,
        PresetSeed,
        PresetStore,
        PresetWriteValidator,
    )
    from tai42_contract.sandbox import Sandbox, SandboxPolicy
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

    def seeds(self) -> list[PresetSeed]:
        """Every declared preset seed. Skeleton-only — the startup/reload seed applier
        consults it, so it is not on the ``AppPresets`` protocol (the register-only
        seam), the same precedent :meth:`write_validator` sets."""
        return self._app._seed_registry.all()

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
    """``app.tool_meta`` — the tool-metadata overlay store (folders + per-tool rows).

    Skeleton-only surface — like ``preset_manager``, it is deliberately not on the
    ``tai42_contract.app.TaiApp`` protocol; the tool_meta routes and the preset
    lifecycle cascade reach it through this concrete instance."""

    @property
    def store(self) -> ToolMetaStore:
        return self._app._tool_meta_store


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
