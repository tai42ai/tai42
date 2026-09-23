"""Tool lookup, the execution-identity authorization seam, and retry-policy resolution over the live server."""

from typing import TYPE_CHECKING, Any

from fastmcp.tools.base import Tool
from fastmcp.tools.tool_transform import TransformedTool
from tai42_contract.tools import ToolRetryPolicy

from tai42_skeleton.tools.binding.errors import UnknownToolError
from tai42_skeleton.tools.binding.state import _ToolBindingBase

if TYPE_CHECKING:
    from tai42_skeleton.authz.identity import CallerIdentity

# Namespace for the synthetic modules that own remote-MCP wrapper functions, so
# a manifest title can never shadow (or be shadowed by) a real importable module
# in ``sys.modules``.
MCP_VIRTUAL_MODULE_PREFIX = "tai42_mcp_virtual."


class _ResolutionMixin(_ToolBindingBase):
    """Resolves a tool name to its bound ``Tool``, authorizes a dispatch, and reads a dispatch's retry policy."""

    # -- execution-identity seam ----------------------------------------------

    @staticmethod
    def _bound_execution_identity() -> "CallerIdentity | None":
        """The execution identity bound to the current fire, or ``None`` outside one.

        Imported inside the function: ``authz`` reaches back into this module.
        """
        from tai42_skeleton.authz.execution_identity import get_execution_identity

        return get_execution_identity()

    async def _authorize_execution_dispatch(
        self, identity: "CallerIdentity", tool_name: str, call_arguments: dict[str, Any]
    ) -> None:
        """Authorize one tool dispatch against the bound execution ``identity``.

        A denial raises ``PermissionDeniedError`` out of the dispatch.

        Call ONLY with a non-``None`` identity. The registries handed to the decision are
        the live ones, so a reload is reflected immediately; an unsettled surface is
        retried once behind the reload gate, since a background fire has no client to
        retry it and would otherwise be lost.
        """
        from tai42_skeleton.app.reload_gate import reload_gate
        from tai42_skeleton.authz.execution import authorize_execution_tool_call
        from tai42_skeleton.authz.resolver import OperationSurfaceUnsettledError

        async def _authorize() -> None:
            await authorize_execution_tool_call(
                identity,
                tool_name,
                call_arguments,
                tool_registry=self._tool_registry,
                preset_manager=self._app.preset_manager,
            )

        try:
            await _authorize()
        except OperationSurfaceUnsettledError:
            async with reload_gate.lock:
                pass
            await _authorize()

    # -- lookup / invocation ---------------------------------------------------

    async def get_tools(self) -> dict[str, Tool]:
        return {t.name: t for t in await self._fast_mcp.list_tools()}

    async def get_tool(self, key: str) -> Tool:
        mcp_tool = await self._fast_mcp.get_tool(key)
        if mcp_tool is None:
            # FastMCP returns None for an unregistered name; the contract promises
            # a Tool, so fail loud with the name rather than leaking None.
            raise UnknownToolError(key)
        return mcp_tool

    async def _resolve_run_target(self, key: str) -> Tool:
        """Resolve ``key`` to its bound tool for a RUN, waiting out an in-flight reload.

        A reload rebuilds the tool registry non-atomically (the FastMCP server is torn
        down and re-registered under the reload gate), so a run that resolves mid-rebuild
        can miss a tool that is bound both before and after — e.g. a backend worker job
        dispatched while the boot self-resync reload is still rebuilding. This is the
        run-surface half of the retriable ``reloading`` contract the HTTP/MCP edges
        answer with a 503/``ToolError``: on a miss, wait for any reload holding the gate
        to release, then re-resolve once. A genuinely unknown tool (no reload in flight)
        still fails fast — the gate is uncontended and the second lookup raises again.
        """
        from tai42_skeleton.app.reload_gate import reload_gate

        try:
            return await self.get_tool(key)
        except UnknownToolError:
            async with reload_gate.lock:
                pass
            return await self.get_tool(key)

    def remove_tool(self, name: str) -> None:
        return self._fast_mcp.local_provider.remove_tool(name)

    def tool_title(self, func) -> str:
        manifest = self._require_manifest()
        module = func.__module__
        if module.startswith(MCP_VIRTUAL_MODULE_PREFIX):
            # Remote-MCP wrappers live in synthetic modules; their title is the
            # manifest title the namespace was minted from.
            return module[len(MCP_VIRTUAL_MODULE_PREFIX) :]
        return manifest.find_title(module)

    async def resolve_retry_policy(self, key: str) -> ToolRetryPolicy | None:
        """The declared retry policy governing a dispatch of ``key``, resolved from the live tool, or ``None``.

        The MCP ``tools/call`` edge has no resolved target in hand (the in-process seam
        reads the policy off the target :meth:`_dispatch_tool` already resolved), so this
        resolves the tool and reads its policy for that edge. An unknown name yields
        ``None`` — the edge's own dispatch surfaces the not-found, never this lookup.
        """
        try:
            mcp_tool = await self._resolve_run_target(key)
        except UnknownToolError:
            return None
        return self._retry_policy_for(key, mcp_tool)

    async def declared_extras(self, name: str) -> frozenset[str]:
        """The door ``extras`` keys a dispatch of ``name`` is declared to read, resolved from the live target.

        An AGENT target declares its keys through its ``extras_keys`` class attribute, so a name that
        resolves to a registered agent answers from that attribute (an agent is not a tool). For a
        tool, declared keys key on the REGISTERED tool name (the ``extras_keys`` parameter of
        ``@app.tools.tool``). A preset-backed dispatch carries its own name but runs the base tool's
        body, so an undeclared transformed tool walks its ``parent_tool`` chain and inherits the
        first declared ancestor's keys — the base tool's declared read surface travels with the
        body. A target with no declaration anywhere in its chain reads no extras (an empty set). An
        unknown name reads none — the caller's own dispatch surfaces the not-found, never this
        lookup.
        """
        agent = self._app._agent_binding.all_agents().get(name)
        if agent is not None:
            return agent.extras_keys
        declared = self._tool_extras_registry.get(name)
        if declared is not None:
            return declared
        try:
            tool_obj: Tool = await self._resolve_run_target(name)
        except UnknownToolError:
            return frozenset()
        while isinstance(tool_obj, TransformedTool):
            tool_obj = tool_obj.parent_tool
            declared = self._tool_extras_registry.get(tool_obj.name)
            if declared is not None:
                return declared
        return frozenset()

    def _retry_policy_for(self, key: str, mcp_tool: Tool) -> ToolRetryPolicy | None:
        """The declared retry policy governing a dispatch of ``key``, or ``None``.

        Declared policies key on the REGISTERED base tool name (the ``retry``
        parameter of ``@app.tools.tool``). A preset-backed dispatch carries its own
        name but runs the base tool's body with baked constants, so an undeclared
        transformed tool walks its ``parent_tool`` chain and inherits the first
        declared ancestor's policy — the idempotency claim travels with the body.
        An extension BRANCH tool inherits nothing: its stack may compose or
        relocate execution the base's declaration never spoke for (a chain re-fires
        OTHER tools), so only an exact-name declaration could ever arm it —
        deliberately conservative on the double-send side.
        """
        policy = self._tool_retry_registry.get(key)
        tool_obj = mcp_tool
        while policy is None and isinstance(tool_obj, TransformedTool):
            tool_obj = tool_obj.parent_tool
            policy = self._tool_retry_registry.get(tool_obj.name)
        return policy
