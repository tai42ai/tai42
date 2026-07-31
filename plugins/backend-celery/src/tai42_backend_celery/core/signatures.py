"""Signature helpers for the extension branch tools.

``add_signature_params`` extends a tool's signature with keyword-only option
parameters, keeping any trailing ``**kwargs`` last.
``exclude_fastmcp_ctx_from_kwargs`` drops the request-scoped FastMCP ``Context``
argument, which cannot cross the queue.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


def _context_param_name(func: Callable[..., Any]) -> str | None:
    """The name of ``func``'s FastMCP ``Context`` parameter, if it has one."""
    from fastmcp import Context
    from fastmcp.utilities.types import find_kwarg_by_type

    return find_kwarg_by_type(func, kwarg_type=Context)


def add_signature_params(
    func: Callable[..., Any],
    additional_opts: dict[str, Any],
    exclude_fastmcp_ctx: bool = False,
) -> inspect.Signature:
    """Return ``func``'s signature extended with keyword-only ``additional_opts``.

    Each ``additional_opts`` entry becomes a keyword-only parameter defaulting
    to ``None`` with the given annotation. With ``exclude_fastmcp_ctx`` the
    FastMCP ``Context`` parameter's annotation is widened to ``Any`` so the
    presented schema does not require a live server context.
    """
    original_sig = inspect.signature(func)
    additional_params = [
        inspect.Parameter(
            name=key,
            kind=inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=annotation,
        )
        for key, annotation in additional_opts.items()
    ]

    if not exclude_fastmcp_ctx:
        params = list(original_sig.parameters.values())
    else:
        context_kwarg = _context_param_name(func)
        params = [
            param.replace(annotation=Any) if param.name == context_kwarg else param
            for param in original_sig.parameters.values()
        ]

    # A VAR_KEYWORD (**kwargs) must stay last, so added params go before it.
    if params and params[-1].kind is inspect.Parameter.VAR_KEYWORD:
        *head, var_keyword = params
        new_params = [*head, *additional_params, var_keyword]
    else:
        new_params = params + additional_params
    return original_sig.replace(parameters=new_params)


def exclude_fastmcp_ctx_from_kwargs(func: Callable[..., Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Return ``arguments`` without ``func``'s FastMCP ``Context`` kwarg (if any)."""
    context_kwarg = _context_param_name(func)
    if context_kwarg and context_kwarg in arguments:
        arguments = {k: v for k, v in arguments.items() if k != context_kwarg}
    return arguments
