"""The ``prometheus_metrics`` tool extension (WRAPPER kind).

Branches a tool into a ``<tool>_prometheus_metrics`` variant that records a call count, an error
count, and a run-time histogram per invocation. Re-presents the wrapped tool's signature unchanged,
adding no control kwargs. Needs the ``prometheus`` extra; fails loudly at import when it is absent.
"""

import inspect
import time

from makefun import create_function
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind

from tai42_toolbox._internal.extensions.prometheus_counters import record_tool_metrics


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="prometheus_metrics")
def prometheus_metrics(func, name, description):
    """Branch ``func`` into a metrics-emitting ``<name>_prometheus_metrics`` variant."""

    async def wrapper(*args, **kwargs):
        start_time = time.time()
        success = True
        try:
            if inspect.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            return result
        except Exception:
            success = False
            raise
        finally:
            duration = time.time() - start_time
            record_tool_metrics(
                title=tai42_app.tools.tool_title(func),
                tool_name=name,
                success=success,
                duration=duration,
            )

    new_name = f"{name}_{prometheus_metrics.__name__}"

    return create_function(
        func_signature=inspect.signature(func),
        func_impl=wrapper,
        func_name=new_name,
        qualname=new_name,
        module_name=func.__module__,
        doc=description,
    )
