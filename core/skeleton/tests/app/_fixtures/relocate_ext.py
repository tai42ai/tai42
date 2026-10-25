"""Fixture extension module: a stub relocating extension ``relocate`` (kind ``BACKEND``).

Its branch presents the wrapped tool's signature (the FastMCP ``Context`` widened to ``Any``, as a
backend engine's branch does), shapes the call's kwargs through the kit's ``prepare_backend_kwargs``
as every backend engine does before it encodes a job, records what that returns in
:data:`RECORDED`, then runs the named tool in process on the recorded arguments minus the platform
stamps, as a worker does.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from fastmcp import Context
from fastmcp.utilities.types import find_kwarg_by_type
from makefun import create_function
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind
from tai42_kit.backend import prepare_backend_kwargs
from tai42_kit.utils.data import makefun_func_name
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

TOOL_NAME_ARG = "relocated_tool"
PLATFORM_STAMPS = frozenset({TOOL_NAME_ARG, WORKER_SECRET_CAPABILITY_ARG})

# Every job payload the branch shaped, in call order.
RECORDED: list[dict[str, Any]] = []


def _presented_signature(func: Callable[..., Any]) -> inspect.Signature:
    signature = inspect.signature(func)
    context_kwarg = find_kwarg_by_type(func, kwarg_type=Context)
    return signature.replace(
        parameters=[
            param.replace(annotation=Any) if param.name == context_kwarg else param
            for param in signature.parameters.values()
        ]
    )


@tai42_app.extensions.extension(kind=ExtensionKind.BACKEND, name="relocate")
def relocate(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
    """Branch ``func`` into ``<name>_relocate``: shape the job payload, record it, run the tool by name."""
    raw_name = f"{name}_relocate"
    safe_name = makefun_func_name(raw_name)

    async def func_impl(*args: Any, **kwargs: Any) -> Any:
        job = await prepare_backend_kwargs(func, TOOL_NAME_ARG, name, kwargs)
        RECORDED.append(dict(job))
        arguments = {key: value for key, value in job.items() if key not in PLATFORM_STAMPS}
        return await tai42_app.tools.run_tool(job[TOOL_NAME_ARG], arguments)

    branch = create_function(
        func_signature=_presented_signature(func).replace(return_annotation=inspect.Signature.empty),
        func_impl=func_impl,
        func_name=safe_name,
        qualname=safe_name,
        module_name=func.__module__,
        doc=description,
    )
    branch.__name__ = raw_name
    return branch
