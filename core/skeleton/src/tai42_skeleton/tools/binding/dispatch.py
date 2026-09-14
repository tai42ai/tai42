"""The shared in-process ``run_tool`` seam — tier fence, dispatch scope, retry,
and the resolved-target invocation."""

import inspect
from typing import Any

from fastmcp.server.dependencies import without_injected_parameters
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.tools.tool_transform import TransformedTool
from fastmcp.utilities.types import get_cached_typeadapter

from tai42_skeleton.agent.binding import _UNSET
from tai42_skeleton.tools.binding.arguments import _validation_wrapper
from tai42_skeleton.tools.binding.resolution import _ResolutionMixin
from tai42_skeleton.tools.binding.result import _serialize_result, _tool_result_value
from tai42_skeleton.tools.context_bridge import bridge_context
from tai42_skeleton.tools.dispatch_scope import dispatch_scope
from tai42_skeleton.tools.retry import dispatch_with_retry
from tai42_skeleton.tools.reveal_gate import InprocessRevealGate, inprocess_reveal_gate
from tai42_skeleton.tools.tier import enforce_run_tier


class _DispatchMixin(_ResolutionMixin):
    """Runs a tool through the single in-process execution seam: fence, scope,
    identity authorization, resolution, retry, and result serialization."""

    async def run_tool(self, key: str, arguments: dict[str, Any], *, offload_sync: bool = False) -> Any:
        """Validate ``arguments`` against ``key``'s signature and invoke it.

        With ``offload_sync`` set AND the resolved tool function being a plain
        (non-coroutine) callable, the sync call runs on a worker thread via
        ``asyncio.to_thread`` instead of inline on the event loop — a blocking
        sync tool then cannot starve the loop (or a co-running supervisor's
        liveness refresh). ``asyncio.to_thread`` copies the current contextvars
        into the thread, so ``without_injected_parameters``' ctx/``Depends``
        resolution still sees the active context. Async tools and the default
        (``offload_sync=False``) path run inline on the event loop.

        A dispatch under a bound execution identity is authorized against it first, on the
        arguments actually about to be fired (:meth:`_authorize_execution_dispatch`).

        The shared in-process execution seam every door flows through: it enters the
        shared :func:`dispatch_scope`, which arms the run lifecycle (the ambient
        invoked-tool deposit, the run-attribution stamp, the turn budget, and — for the
        OUTERMOST registered-preset dispatch — the preset version stamp and the
        runs-index record that opens the run's trace root). The MCP ``tools/call`` edge
        enters the SAME scope via ``DispatchScopeMiddleware``. Retry and bound-identity
        authorization live in :meth:`_dispatch_tool`, so a retried call is one logical
        dispatch inside one scope."""
        # Run-time tier fence: a ``fenced``/``secret`` tool — or a preset/branch over one —
        # runs only for an administrator. Enforced here at the shared in-process seam every
        # door flows through, before argument work or the invoked-tool deposit, so a refused
        # run has no visible effect; a detached fire resolves its bound execution identity,
        # so its refusal is a recorded failed run. The MCP edge never reaches this seam and
        # fences at its own middleware. A non-fenced tool resolves no caller (a dict read).
        await enforce_run_tier(self._app, key)
        # An agent run tool's re-dispatch (e.g. a chain TRANSFORMER re-invoking it by
        # name) materializes the _UNSET sentinel for optionals the caller never
        # supplied; strip it here so the set-fields-only contract holds and the tool's
        # own defaults apply instead of a sentinel failing validation. No external
        # caller can produce _UNSET, so this is a no-op for ordinary arguments.
        arguments = {name: value for name, value in arguments.items() if value is not _UNSET}
        async with dispatch_scope(self._app, key, arguments) as scope:
            result = await self._dispatch_tool(key, arguments, offload_sync=offload_sync)
            scope.observe(result)
            return result

    async def _dispatch_tool(self, key: str, arguments: dict[str, Any], *, offload_sync: bool) -> Any:
        """Resolve ``key`` and invoke it under any bound execution identity, arguments
        already stripped of the ``_UNSET`` sentinel. The turn budget (:meth:`run_tool`)
        wraps this dispatch.

        A tool that DECLARED a retry policy has its invocation re-fired through
        :func:`dispatch_with_retry` — inside the runs-index record and attribution
        stamp (one row, one logical dispatch, whatever the attempt count) and after
        the authorization + resolution below, which run ONCE per dispatch: the
        decision was made for this call, and every attempt re-fires the same
        resolved target. No policy = the loop degenerates to a single plain await."""
        # Execution-identity seam: decided at INVOCATION, before the tool is resolved, on
        # the exact arguments this call fires. With no identity bound it is a contextvar
        # read and the path below is untouched.
        execution_identity = self._bound_execution_identity()
        if execution_identity is not None:
            await self._authorize_execution_dispatch(execution_identity, key, arguments)
        mcp_tool = await self._resolve_run_target(key)
        retry_policy = self._retry_policy_for(key, mcp_tool)
        if isinstance(mcp_tool, TransformedTool):
            # A prebuilt transformed tool (e.g. a preset) has no plain callable to
            # validate against — its ``fn`` takes one opaque ``**kwargs`` and its
            # typed contract lives on the object. Run it through the tool's OWN
            # schema-validating ``run``, which applies the baked hidden constants
            # and REJECTS a caller that passes a baked key. Bridge the in-process
            # Context the same way the callable path does.

            async def run_transformed_attempt() -> Any:
                # The preset's forwarding fn re-enters the parent tool's ``run`` — and
                # so its secret-revealing ``convert_result`` — in-process. Arm the gate
                # so that reveal is suppressed and any secret-bearing return is carried
                # out wrapper-intact (each downstream door then masks or reveals its own
                # copy), instead of the ``ToolResult`` handing back already-revealed
                # plaintext a recorder can no longer mask. A fresh gate per attempt: a
                # failed attempt's stale gate state must never leak into the next.
                gate = InprocessRevealGate()
                token = inprocess_reveal_gate.set(gate)
                try:
                    with bridge_context(self._app.fastmcp):
                        result = await mcp_tool.run(arguments)
                finally:
                    inprocess_reveal_gate.reset(token)
                if gate.has_park:
                    # An async ask_user parked the caller: return the sentinel object by
                    # TYPE (never the flattened ToolResult), so the preset path recognizes
                    # the park exactly as the direct-run path does. A park is a RETURN —
                    # never a retried failure.
                    return gate.park
                if gate.has_payload:
                    return gate.payload
                return _tool_result_value(result)

            return await dispatch_with_retry(key, retry_policy, run_transformed_attempt)
        if not isinstance(mcp_tool, FunctionTool):
            raise RuntimeError(f"Tool {key!r} has no callable body and cannot be run directly.")

        # ``without_injected_parameters`` strips the fastmcp Context / ``Depends``
        # params from the signature AND, when called, resolves and injects them
        # before invoking ``fn``. Validation runs against that stripped signature,
        # and invocation goes through this same wrapper — calling the raw ``fn``
        # would bypass injection (a ctx tool raises TypeError; a Depends tool
        # receives the unresolved sentinel object).
        fn = mcp_tool.fn
        resolved_fn = without_injected_parameters(fn)
        # A coroutine tool never blocks the loop, so it is never offloaded; only a
        # sync callable is, and only when the caller opted in. ``iscoroutinefunction``
        # tracks the wrapped async-ness through ``without_injected_parameters``.
        offload = offload_sync and not inspect.iscoroutinefunction(resolved_fn)

        # A stable per-(resolved_fn, offload) wrapper: fastmcp caches
        # ``resolved_fn``, so this reuses one makefun wrapper across calls and
        # ``get_cached_typeadapter`` below hits its process-global LRU instead of
        # thrashing it with a per-call throwaway.
        safe_wrapper = _validation_wrapper(resolved_fn, offload)

        async def run_validated_adapter(args):
            type_adapter = get_cached_typeadapter(safe_wrapper)
            result = type_adapter.validate_python(args)
            if inspect.isawaitable(result):
                return await result
            return result

        async def run_function_attempt() -> Any:
            # In-process Context bridge: this invocation has no connected client, so
            # an injected ``ctx.elicit()`` routes to the interactions ``ask_user``
            # waiter and ``ctx.sample()`` falls back to the platform LLM. The bridge
            # no-ops when a live request context is already active, so a capable
            # client still resolves in-client. ``asyncio.to_thread`` copies
            # contextvars into the offload thread, so the pushed context reaches the
            # sync path too.
            with bridge_context(self._app.fastmcp):
                return await run_validated_adapter(arguments)

        result = await dispatch_with_retry(key, retry_policy, run_function_attempt)
        return _serialize_result(result)
