"""The ``monitor`` builtin tool extension.

A tool extension extends a single tool (plugins extend the platform). ``monitor``
is a WRAPPER-kind tool extension: opt in per tool via the manifest
``extensions`` map (``extensions: {toolname: [monitor]}``) and every standalone
call of the branch tool is traced as one live ``SpanKind.TOOL`` span.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from makefun import create_function
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind
from tai42_contract.monitoring import MonitoringLevel, SpanKind
from tai42_contract.secrets import mask_secrets
from tai42_kit.utils.data import makefun_func_name

from tai42_skeleton.monitoring import get_monitoring


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="monitor")
def monitor(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
    """Trace a standalone tool call as one live ``SpanKind.TOOL`` span.

    ``monitor`` takes no author config: it is a config-agnostic three-argument
    factory, so the apply site calls it without a config argument and rejects any
    config bound to it.

    A WRAPPER tool extension: it presents the wrapped tool's input signature
    unchanged (re-presented with ``makefun`` so the branch is schema-identical to
    the original) and branches it to a new ``<name>_monitor`` tool. The span times
    the call live, so it carries real wall-clock latency. When a flow/agent trace
    is already active its own seam owns the tool span, so this suppresses itself
    and just runs the tool — it emits only for a genuinely standalone invocation.
    Errors propagate in both the emitting and the suppressed path; on error the
    span is marked ``MonitoringLevel.ERROR`` before the exception is re-raised.
    """

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        async def _call() -> Any:
            if inspect.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            # A sync tool runs in a worker thread (as fastmcp's own threadpool
            # offload would) so a blocking body cannot starve the event loop this
            # async branch runs on. ``asyncio.to_thread`` copies the current
            # contextvars, so the active trace/monitoring state propagates.
            return await asyncio.to_thread(func, *args, **kwargs)

        writer = get_monitoring().writer
        if writer.current_trace_id() is not None:
            return await _call()

        # The span is a recorder, not a live-caller door: any wrapped secret in the
        # arguments or the result is masked to the placeholder before it is emitted.
        with writer.start_span(
            name=name,
            kind=SpanKind.TOOL,
            input={"args": mask_secrets(args), "kwargs": mask_secrets(kwargs)},
        ) as span:
            try:
                result = await _call()
            except Exception as e:
                span.update(level=MonitoringLevel.ERROR, status_message=str(e))
                raise
            span.update(output=mask_secrets(result))
            return result

    raw_name = f"{name}_{monitor.__name__}"
    safe_name = makefun_func_name(raw_name)

    branch = create_function(
        func_signature=inspect.signature(func),
        func_impl=wrapper,
        func_name=safe_name,
        qualname=safe_name,
        module_name=func.__module__,
        doc=description,
    )
    # The branch loop registers a branch by its ``__name__``; restore the raw
    # intended name so a hyphenated name isn't collapsed to its makefun alias.
    branch.__name__ = raw_name
    return branch
