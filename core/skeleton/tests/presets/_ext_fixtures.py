"""Schema-preserving wrapper extensions for the preset-engine tests.

Listed as the engine tests' ``extensions_modules`` entry (kept separate from the
``tools`` module so no single module is imported by two manifest sections — a
double import would re-bind the tools module's tools, a hard collision under the
server's ``on_duplicate="error"``). A preset's extension COMBOS attach these two
independent wrappers as branches. ``jqpass`` is a transformer presenting the wrapped
tool's parameters plus a keyword-only ``jq_expression: TemplatedText``.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from makefun import create_function
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind
from tai42_contract.template import TemplatedText


def _marking(func: Callable[..., Any], marker: str) -> Callable[..., Any]:
    """A wrapper of ``func`` appending ``marker`` to a string result, awaiting a coroutine function's result."""

    def mark(result: Any) -> Any:
        return f"{result}{marker}" if isinstance(result, str) else result

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def awaiting(*args, **kwargs):
            return mark(await func(*args, **kwargs))

        return awaiting

    @functools.wraps(func)
    def calling(*args, **kwargs):
        return mark(func(*args, **kwargs))

    return calling


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="exta")
def exta(func, name, desc):
    """Independent order-marker wrapper: append ``|a`` to a string result;
    schema-preserving so it branches any base cleanly."""

    variant = _marking(func, "|a")
    variant.__name__ = f"{name}_exta"
    variant.__qualname__ = variant.__name__
    return variant


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="extb")
def extb(func, name, desc):
    """Independent order-marker wrapper: append ``|b`` to a string result."""

    variant = _marking(func, "|b")
    variant.__name__ = f"{name}_extb"
    variant.__qualname__ = variant.__name__
    return variant


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="shaped", declares_output_schema=True)
def shaped(func, name, desc):
    """A wrapper that declares the wrapped tool's output shape (a neutral stand-in for any such extension)."""

    @functools.wraps(func)
    def variant(*args, **kwargs):
        return func(*args, **kwargs)

    variant.__name__ = f"{name}_shaped"
    variant.__qualname__ = variant.__name__
    return variant


@tai42_app.extensions.extension(kind=ExtensionKind.TRANSFORMER, name="jqpass")
def jqpass(func, orig_name, orig_desc):
    """Branch ``func`` into ``<orig_name>_jqpass``: its parameters plus a keyword-only ``jq_expression``."""
    sig = inspect.signature(func)
    params = [
        *sig.parameters.values(),
        inspect.Parameter("jq_expression", inspect.Parameter.KEYWORD_ONLY, annotation=TemplatedText),
    ]

    async def func_impl(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("jq_expression")
        result = func(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    return create_function(
        func_signature=sig.replace(parameters=params, return_annotation=Any),
        func_impl=func_impl,
        func_name=f"{orig_name}_jqpass",
        module_name=func.__module__,
        doc=f"jqpass of {orig_name!r}.\n\n{orig_desc}",
    )
