"""Read/write the live epoch's serving core through the per-epoch forwarding accessors."""

from typing import TYPE_CHECKING

from fastmcp import FastMCP

from tai42_skeleton.app.epoch import current_epoch, current_epoch_or_none
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.extensions import ExtensionRegistry
from tai42_skeleton.tools import ToolRegistry

if TYPE_CHECKING:
    from tai42_skeleton.agent.binding import AgentBinding
    from tai42_skeleton.app.http import HttpSurface
    from tai42_skeleton.app.server import ServingCore
    from tai42_skeleton.app.sessions import SessionRegistry
    from tai42_skeleton.backend.registry import BackendHolder
    from tai42_skeleton.backup import BackupRegistry
    from tai42_skeleton.channels.registry import ChannelRegistry
    from tai42_skeleton.conversations.target_validators import TargetBindValidatorRegistry
    from tai42_skeleton.manifest import Manifest
    from tai42_skeleton.presets.base_tool_config import PresetInputSchemaSupportRegistry
    from tai42_skeleton.presets.manager import PresetManager
    from tai42_skeleton.presets.seeds import PresetSeedRegistry
    from tai42_skeleton.presets.write_validators import PresetWriteValidatorRegistry
    from tai42_skeleton.sandbox import SandboxHolder
    from tai42_skeleton.states.seeds import StateTemplateSeedRegistry
    from tai42_skeleton.states.service import (
        StatesAttachReconcilerRegistry,
        StatesAttachValidatorRegistry,
        StatesConsumerListerRegistry,
        StatesService,
    )
    from tai42_skeleton.template import ResourceManager
    from tai42_skeleton.tools import ToolRefsRegistry, ToolRetryRegistry, ToolTierRegistry
    from tai42_skeleton.tools.binding import ToolBinding
    from tai42_skeleton.tools.delete_referees import ToolDeleteRefereeRegistry
    from tai42_skeleton.tools.detach_referees import StateTemplateDetachRefereeRegistry
    from tai42_skeleton.tools.rename_referees import ToolRenameRefereeRegistry
    from tai42_skeleton.webhooks.registry import WebhookVerifierRegistry


class ServingCoreAccessMixin(LifecycleState):
    # -- per-epoch serving core (forwarding reads) -----------------------------
    # Every collaborator the lifecycle swaps per epoch is read through the live
    # serving generation's ``ServingCore``: the half-built core during a build (so
    # ``start()`` and the epoch handlers register into the epoch being built), else the
    # installed epoch's core. A reload that swaps in a fresh epoch is therefore visible
    # at every read site with no rebinding.

    @property
    def _serving_core(self) -> "ServingCore":
        if self._building is not None:
            return self._building
        core = current_epoch().core
        if core is None:
            raise RuntimeError("the live epoch has no serving core installed")
        return core

    @property
    def _live_serving_core(self) -> "ServingCore":
        """The LIVE epoch's core once one is installed; the pre-boot core before that.

        Request-path reads of per-epoch provider state resolve THIS, not the
        ``_building``-first :attr:`_serving_core`. During a reload a live epoch is ALWAYS
        installed (``current_epoch()`` is the outgoing generation until the atomic swap),
        so a request that interleaves with an in-flight build's serving-app enter sees the
        LIVE core and NEVER the generation under construction — a build that then FAILS can
        never leave a surviving epoch's memoized verifier bound onto the discarded
        generation's provider instances (the zero-mutation invariant). Before the boot
        epoch is installed there is no live generation, so this falls back to the sole
        pre-boot core (``_building``), which registration and the pre-boot harness read;
        no request is served in that window, so the being-built generation is never
        exposed here."""
        epoch = current_epoch_or_none()
        if epoch is not None and epoch.core is not None:
            return epoch.core
        if self._building is not None:
            return self._building
        raise RuntimeError("the live epoch has no serving core installed")

    # Generation state on the epoch's core, reached read/write through these forwarding
    # properties: ``start()`` and the runtime MCP mutators write the epoch under
    # construction during a build (``_building``) and the live epoch's core otherwise, so
    # a failed build never touches the serving generation. ``_manifest is None`` on a
    # fresh core is the pre-boot no-op contract the registration decorators check.

    @property
    def _manifest(self) -> "Manifest | None":
        return self._serving_core._manifest

    @_manifest.setter
    def _manifest(self, value: "Manifest | None") -> None:
        self._serving_core._manifest = value

    @property
    def _tool_registry(self) -> ToolRegistry:
        return self._serving_core._tool_registry

    @_tool_registry.setter
    def _tool_registry(self, value: ToolRegistry) -> None:
        self._serving_core._tool_registry = value

    @property
    def _extension_registry(self) -> ExtensionRegistry:
        return self._serving_core._extension_registry

    @_extension_registry.setter
    def _extension_registry(self, value: ExtensionRegistry) -> None:
        self._serving_core._extension_registry = value

    @property
    def _failed_mcps(self) -> dict[str, str]:
        return self._serving_core._failed_mcps

    @_failed_mcps.setter
    def _failed_mcps(self, value: dict[str, str]) -> None:
        self._serving_core._failed_mcps = value

    @property
    def _mcp_bound_tools(self) -> dict[str, set[str]]:
        return self._serving_core._mcp_bound_tools

    @_mcp_bound_tools.setter
    def _mcp_bound_tools(self, value: dict[str, set[str]]) -> None:
        self._serving_core._mcp_bound_tools = value

    @property
    def _mcp_preset_conflicts(self) -> dict[str, set[str]]:
        return self._serving_core._mcp_preset_conflicts

    @_mcp_preset_conflicts.setter
    def _mcp_preset_conflicts(self, value: dict[str, set[str]]) -> None:
        self._serving_core._mcp_preset_conflicts = value

    @property
    def _resource_manager_cache(self) -> "ResourceManager | None":
        return self._serving_core._resource_manager_cache

    @_resource_manager_cache.setter
    def _resource_manager_cache(self, value: "ResourceManager | None") -> None:
        self._serving_core._resource_manager_cache = value

    @property
    def _fast_mcp(self) -> FastMCP:
        return self._serving_core._fast_mcp

    @property
    def _session_registry(self) -> "SessionRegistry":
        return self._serving_core._session_registry

    @property
    def _tool_binding(self) -> "ToolBinding":
        return self._serving_core._tool_binding

    @property
    def _agent_binding(self) -> "AgentBinding":
        return self._serving_core._agent_binding

    @property
    def _backend_holder(self) -> "BackendHolder":
        return self._serving_core._backend_holder

    @property
    def _sandbox_holder(self) -> "SandboxHolder":
        return self._serving_core._sandbox_holder

    @property
    def _http_surface(self) -> "HttpSurface":
        return self._serving_core._http_surface

    @property
    def _webhook_verifier_registry(self) -> "WebhookVerifierRegistry":
        return self._serving_core._webhook_verifier_registry

    @property
    def _channel_registry(self) -> "ChannelRegistry":
        return self._serving_core._channel_registry

    @property
    def _write_validator_registry(self) -> "PresetWriteValidatorRegistry":
        return self._serving_core._write_validator_registry

    @property
    def _target_validator_registry(self) -> "TargetBindValidatorRegistry":
        return self._serving_core._target_validator_registry

    @property
    def _input_schema_support_registry(self) -> "PresetInputSchemaSupportRegistry":
        return self._serving_core._input_schema_support_registry

    @property
    def _registration_tier_registry(self) -> "ToolTierRegistry":
        return self._serving_core._registration_tier_registry

    @property
    def _tool_refs_registry(self) -> "ToolRefsRegistry":
        return self._serving_core._tool_refs_registry

    @property
    def _tool_retry_registry(self) -> "ToolRetryRegistry":
        return self._serving_core._tool_retry_registry

    @property
    def _rename_referee_registry(self) -> "ToolRenameRefereeRegistry":
        return self._serving_core._rename_referee_registry

    @property
    def _delete_referee_registry(self) -> "ToolDeleteRefereeRegistry":
        return self._serving_core._delete_referee_registry

    @property
    def _detach_referee_registry(self) -> "StateTemplateDetachRefereeRegistry":
        return self._serving_core._detach_referee_registry

    @property
    def _seed_registry(self) -> "PresetSeedRegistry":
        return self._serving_core._seed_registry

    @property
    def _backup_registry(self) -> "BackupRegistry":
        return self._serving_core._backup_registry

    @property
    def _states_service(self) -> "StatesService":
        return self._serving_core._states_service

    @property
    def _states_attach_validators(self) -> "StatesAttachValidatorRegistry":
        return self._serving_core._states_attach_validators

    @property
    def _states_attach_reconcilers(self) -> "StatesAttachReconcilerRegistry":
        return self._serving_core._states_attach_reconcilers

    @property
    def _states_consumer_listers(self) -> "StatesConsumerListerRegistry":
        return self._serving_core._states_consumer_listers

    @property
    def _states_template_seeds(self) -> "StateTemplateSeedRegistry":
        return self._serving_core._states_template_seeds

    @property
    def _preset_manager(self) -> "PresetManager":
        return self._serving_core._preset_manager
