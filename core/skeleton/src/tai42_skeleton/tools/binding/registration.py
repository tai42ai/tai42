"""The ``@app.tools.tool`` / ``toolkit`` / ``mcp_tools`` registration decorators and the tool-info registry surface."""

import logging
import sys
import types
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fastmcp.tools.base import Tool
from tai42_contract.manifest import ExtensionElement, TaiMCPConfig
from tai42_contract.tools import ToolRetryPolicy

from tai42_skeleton.tools.adapters.lc_tool_to_func import lc_tool_to_func
from tai42_skeleton.tools.binding.branch import _BranchBindingMixin
from tai42_skeleton.tools.binding.resolution import MCP_VIRTUAL_MODULE_PREFIX

if TYPE_CHECKING:
    from tai42_contract.app import RouteAction
    from tai42_contract.tools import ToolRefsExtractor

logger = logging.getLogger(__name__)


class _RegistrationMixin(_BranchBindingMixin):
    """Registers tools/toolkits/remote-MCP tools onto the live server and exposes the tool-info registry surface."""

    def tool(
        self,
        *args,
        force=False,
        tool_refs: "ToolRefsExtractor | None" = None,
        retry: ToolRetryPolicy | None = None,
        tier: "RouteAction | None" = None,
        **kwargs,
    ) -> Any:
        func_to_register = None
        decorator_args = args

        # A prebuilt FastMCP ``Tool`` object (e.g. a preset's baked transform) is
        # registered DIRECTLY, preserving its typed schema — it is not callable, so
        # detect it explicitly alongside the callable/decorator forms.
        if args and (callable(args[0]) or isinstance(args[0], Tool)):
            func_to_register = args[0]
            decorator_args = args[1:]

        def decorator(func):
            if not self._manifest:
                return func

            name = kwargs.get("name") or (func.name if isinstance(func, Tool) else func.__name__)
            module = "" if isinstance(func, Tool) else func.__module__
            if not (force or self._manifest.should_include_tool(name, module)):
                return func
            # A manifest-excluded tool registers nothing (returned above); only an
            # included tool's declared refs extractor lands, keyed by its bound name.
            if tool_refs is not None:
                self._tool_refs_registry.register(name, tool_refs)
            # Likewise for the declared retry policy — validated here so a malformed
            # declaration (a dict is accepted and coerced) fails loudly at bind time,
            # never silently at dispatch.
            if retry is not None:
                self._tool_retry_registry.register(name, ToolRetryPolicy.model_validate(retry))
            # Likewise for the declared registration tier — the declarative form of
            # ``app.tools.register_tier``, keyed by the bound name so both the authoring
            # gate and the run-time fence read it.
            if tier is not None:
                self._registration_tier_registry.register(name, tier)
            return self.bind_tool_func(*decorator_args, **kwargs)(func)

        if func_to_register is not None:
            return decorator(func_to_register)

        return decorator

    def tool_refs_extractor(self, name: str) -> "ToolRefsExtractor | None":
        """The declared tool-references extractor a base tool registered under ``name``.

        ``None`` when it declared none.
        """
        return self._tool_refs_registry.get(name)

    def toolkit(self, *args, **kwargs):
        func_to_register = None
        decorator_args = args

        if args and callable(args[0]):
            func_to_register = args[0]
            decorator_args = args[1:]

        def decorator(func):
            if not self._manifest:
                return func

            for t in func().get_tools():
                toolkit_name = kwargs.get("name", func.__name__)
                name = self.normalized_name(toolkit_name, t.name)

                if not self._manifest.should_include_tool(name=name, module=func.__module__):
                    continue

                adapted_tool = lc_tool_to_func(t, name=name, module=func.__module__)

                tool_kwargs = kwargs.copy()
                tool_kwargs["name"] = name
                self.bind_tool_func(*decorator_args, **tool_kwargs)(adapted_tool)

            return func

        if func_to_register:
            return decorator(func_to_register)

        return decorator

    def mcp_tools(self, config: TaiMCPConfig, tools) -> None:
        manifest = self._require_manifest()
        virtual_module = MCP_VIRTUAL_MODULE_PREFIX + config.title
        if virtual_module not in sys.modules:
            sys.modules[virtual_module] = types.ModuleType(virtual_module)

        # Reset so a reload cleanly re-tracks what this MCP binds now.
        self._mcp_bound_tools[config.title] = set()
        self._app._mcp_preset_conflicts[config.title] = set()

        # Resolve the schema-depth bound ONCE, up front and outside the per-tool skip
        # guard below, so a malformed TAI_MCP_SCHEMA_MAX_DEPTH surfaces loudly as a
        # config error instead of being caught per tool and mis-logged as every tool
        # advertising an unusable schema.
        from tai42_skeleton.settings.mcp_settings import mcp_dispatch_settings

        schema_max_depth = mcp_dispatch_settings().schema_max_depth

        for t in tools:
            name = self.normalized_name(config.title, t.name)
            if not manifest.should_include_mcp_tool(name=name, title=config.title):
                continue

            if self._app.preset_manager.is_registered(name):
                # A registered preset already owns this name (the sanctioned
                # create-over-a-requested-unbound-MCP-name flow). Binding the
                # returning server's tool would clobber it — a hard error under
                # ``on_duplicate="error"`` — so skip it and record the conflict for
                # the reload result to surface.
                self._app._mcp_preset_conflicts[config.title].add(name)
                logger.warning("mcp %r tool %r skipped: a registered preset owns the name", config.title, name)
                continue

            from tai42_skeleton.tools.adapters.mcp_tool_to_func import mcp_tool_to_func

            try:
                adapted_tool = mcp_tool_to_func(
                    config=config, tool=t, name=name, module=virtual_module, schema_max_depth=schema_max_depth
                )
            except Exception:
                # A single tool advertising an unusable schema (empty anyOf/oneOf,
                # over-depth nesting) is SKIPPED with a loud log, never allowed to
                # take down the whole binding pass / startup — every other tool this
                # server advertises still binds. ``bind_tool_func`` stays OUTSIDE
                # this guard so a genuine registration bug still raises loudly.
                logger.error(
                    "MCP server %r advertised tool %r with an unusable schema; "
                    "skipping this tool (every other tool still binds)",
                    config.title,
                    t.name,
                    exc_info=True,
                )
                continue
            self.bind_tool_func(owner=config.title)(adapted_tool)

    def register_tool_info(self, name: str, combos: Sequence[Sequence[ExtensionElement]] | None = None) -> None:
        self._tool_registry.register_tool(name, combos)

    def unregister_tool_info(self, name: str) -> None:
        self._tool_registry.unregister_tool(name)

    def unregister_tool_base(self, tool_name: str) -> list[str]:
        return self._tool_registry.unregister_tool_base(tool_name)

    def base_of(self, name: str) -> str:
        return self._tool_registry.base_of(name)

    def is_branch(self, name: str) -> bool:
        return self._tool_registry.is_branch(name)

    def mcp_bound_names(self, title: str) -> frozenset[str]:
        """The tool names currently bound by the MCP server ``title``.

        A read-only snapshot of the per-title bound-tool map (empty for an unknown title).
        """
        return frozenset(self._mcp_bound_tools.get(title, set()))

    def available_extensions(self) -> list[dict[str, str]]:
        return self._extension_registry.available_extensions()

    @staticmethod
    def normalized_name(prefix: str, name: str) -> str:
        name = name.lower().replace("-", "_")
        prefix = prefix.lower().replace("-", "_")
        # Prefix unless already prefixed. The check must respect the ``_``
        # separator: ``slacker`` under prefix ``slack`` is NOT already prefixed
        # (a bare ``startswith(prefix)`` would wrongly treat it as such), so it
        # becomes ``slack_slacker``.
        already_prefixed = name == prefix or name.startswith(prefix + "_")
        return name if not prefix or already_prefixed else f"{prefix}_{name}"
