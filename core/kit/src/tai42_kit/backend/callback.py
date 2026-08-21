"""Callback glue — chain a follow-up tool after a backend task runs.

:class:`CallbackSchema` completes the contract field shape with render methods
that reach the host's resource manager. ``callback_execution`` gates a task
result on the rendered condition, transforms it with the rendered expression,
and optionally runs a follow-up tool. ``prepare_backend_kwargs`` strips the
FastMCP context and injects the tool name before a backend dispatch.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_contract.access_control.context import caller_may_read_secrets
from tai42_contract.app import tai42_app
from tai42_contract.backend import CallbackSchema as CallbackFields

from tai42_kit.utils.data import run_jq_first
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run
from tai42_kit.utils.lc.signature_util import exclude_fastmcp_ctx_from_kwargs
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG, bind_worker_secret_capability


class CallbackSchema(CallbackFields):
    """The contract callback field shape plus render methods that reach the live
    resource manager."""

    async def rendered_condition(self) -> str:
        return await tai42_app.storage.resource_manager.render_by_id_or_content(
            content=self.condition,
            template_id=self.condition_id,
            kwargs=self.condition_kwargs,
        )

    async def rendered_expr(self) -> str:
        return await tai42_app.storage.resource_manager.render_by_id_or_content(
            content=self.expr,
            template_id=self.expr_id,
            kwargs=self.expr_kwargs,
        )


async def prepare_backend_kwargs(
    func: Callable[..., Any], tool_name_arg: str, tool_name: str, kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Strip the FastMCP context kwarg, inject the tool name for dispatch, and stamp
    the submitting caller's secret-read capability so the worker binds it for the job.

    Runs in the submitter's request context, so :func:`caller_may_read_secrets` reads the
    submitter's own admin verdict; stamped AFTER the caller's arguments are stripped, so a
    caller can never forge a higher capability."""
    kwargs = exclude_fastmcp_ctx_from_kwargs(func, kwargs)
    kwargs[tool_name_arg] = tool_name
    kwargs[WORKER_SECRET_CAPABILITY_ARG] = caller_may_read_secrets()
    return kwargs


async def callback_execution(result: Any, callback: CallbackSchema) -> Any:
    """Run ``callback`` over ``result``: gate on the condition, transform with
    the expression, then run the follow-up tool (when one is named)."""
    cond = await callback.rendered_condition()
    if cond:
        # An empty pipeline is falsy → skip, matching the ``if not cond_output`` gate.
        cond_output = await run_jq_first(cond, result, default=None)
        if not cond_output:
            return None

    expr = await callback.rendered_expr()
    # Empty expr is not an error: ``get_compiled_jq("")`` raises, so it yields {}.
    # An empty PIPELINE from a non-empty expr also yields {} (default). Evaluated
    # through ``run_jq_first`` so the JQ_TIMEOUT_SECONDS budget holds.
    expr_output = (await run_jq_first(expr, result, default={})) if expr else {}

    if callback.tool:
        # A worker executes a dequeued callback with no live caller holding a
        # connection, so the turn budget does not apply, and no HTTP request bound
        # the secret-read capability — the worker binds it here to the gate state.
        detached_token = mark_detached_run()
        try:
            with bind_worker_secret_capability():
                return await tai42_app.tools.run_tool(callback.tool, expr_output, offload_sync=True)
        finally:
            reset_detached_run(detached_token)
    return expr_output
