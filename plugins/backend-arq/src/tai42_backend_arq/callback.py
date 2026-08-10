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

from tai42_contract.app import tai42_app
from tai42_contract.backend import CallbackSchema as CallbackFields
from tai42_kit.utils.data.jq_util import get_compiled_jq

from tai42_backend_arq.signatures import exclude_fastmcp_ctx_from_kwargs


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
    """Strip the FastMCP context kwarg and inject the tool name for dispatch."""
    kwargs = exclude_fastmcp_ctx_from_kwargs(func, kwargs)
    kwargs[tool_name_arg] = tool_name
    return kwargs


async def callback_execution(result: Any, callback: CallbackSchema) -> Any:
    """Run ``callback`` over ``result``: gate on the condition, transform with
    the expression, then run the follow-up tool (when one is named)."""
    cond = await callback.rendered_condition()
    if cond:
        cond_output = get_compiled_jq(cond).input(result).first()
        if not cond_output:
            return None

    expr = await callback.rendered_expr()
    # Empty expr is not an error: ``get_compiled_jq("")`` raises, so it yields {}.
    expr_output = get_compiled_jq(expr).input(result).first() if expr else {}

    if callback.tool:
        return await tai42_app.tools.run_tool(callback.tool, expr_output)
    return expr_output
