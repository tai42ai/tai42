"""The beta fixture's wrapper tool extension.

Registers at import via ``@tai42_app.extensions.extension``. It branches a tool
into a ``<name>_beta_marker`` variant that runs the wrapped tool and stamps a
marker key onto its dict result, preserving the base tool's input schema
(WRAPPER kind). A genuinely functional extension so the marketplace's item-level
``kind`` facet has an extension to filter on."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="beta_marker")
def beta_marker(func: Callable[..., Any], name: str, description: str) -> Callable[..., Any]:
    """Branch ``func`` into a ``<name>_beta_marker`` variant.

    The variant runs the wrapped tool and, when its result is a mapping, adds a
    ``beta_marker`` key naming the base tool; a non-mapping result passes through
    unchanged. The input schema is preserved, so the branch is schema-compatible
    with its base."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if inspect.iscoroutinefunction(func):
            result = await func(*args, **kwargs)
        else:
            result = func(*args, **kwargs)
        if isinstance(result, dict):
            return {**result, "beta_marker": name}
        return result

    new_name = f"{name}_beta_marker"
    wrapper.__name__ = new_name
    wrapper.__qualname__ = new_name
    wrapper.__doc__ = f"{description}\n\nStamps a beta marker key onto the wrapped tool's result."
    return wrapper
