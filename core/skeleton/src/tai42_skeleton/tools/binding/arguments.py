"""Shape a client call's arguments by parameter name and build the makefun validation wrapper ``run_tool`` uses."""

import asyncio
import inspect
from collections.abc import Callable
from functools import lru_cache
from typing import Any, cast

from makefun import create_function
from tai42_kit.utils.data import makefun_func_name

from tai42_skeleton.agent.binding import _UNSET


def _named_call_arguments(
    signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """A client-tool call's arguments keyed by PARAMETER NAME.

    The shape the execution-identity decision reads them in. Positionals are
    bound through ``signature``, a ``**kwargs`` catch-all is flattened
    back in, ``*args`` is dropped, and the :data:`_UNSET` sentinel is stripped — so the
    decision sees exactly the set-fields-only argument set ``run_tool`` authorizes.
    """
    bound = signature.bind_partial(*args, **kwargs)
    arguments = dict(bound.arguments)
    for name, param in signature.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            arguments.update(arguments.pop(name, {}))
        elif param.kind is inspect.Parameter.VAR_POSITIONAL:
            arguments.pop(name, None)
    return {name: value for name, value in arguments.items() if value is not _UNSET}


@lru_cache(maxsize=2048)
def _validation_wrapper(resolved_fn: Callable[..., Any], offload: bool) -> Callable[..., Any]:
    """A stable makefun wrapper presenting ``resolved_fn``'s signature.

    Its body resolves and invokes ``resolved_fn`` (offloading a sync call to a
    worker thread when ``offload`` is set). Keyed on ``(resolved_fn, offload)``
    and cached module-wide. ``resolved_fn`` is
    fastmcp's process-cached ``without_injected_parameters`` wrapper — a stable
    object per tool — so ``run_tool`` reuses one wrapper across calls instead of
    compiling a fresh function each time. That keeps fastmcp's process-global
    ``get_cached_typeadapter`` LRU hitting on the same wrapper rather than
    thrashing it with a per-call throwaway.
    """

    async def safe_impl(**kwargs):
        result = await asyncio.to_thread(resolved_fn, **kwargs) if offload else resolved_fn(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    return create_function(
        inspect.signature(resolved_fn),
        # makefun's ``func_impl`` is annotated ``Callable[[Any], Any]`` but it
        # accepts any callable (it drives the separate signature above); our
        # **kwargs impl is valid at runtime.
        cast(Callable[[Any], Any], safe_impl),
        # ``resolved_fn.__name__`` may be a branch's raw non-identifier name (a
        # hyphenated / leading-digit tool + extension); normalize it so makefun
        # emits a compilable ``def`` for the cosmetic wrapper name.
        func_name=makefun_func_name(resolved_fn.__name__),
        # makefun's ``doc`` is annotated ``str`` but accepts ``None`` (its own
        # default), falling back to the impl's docstring.
        doc=cast(str, resolved_fn.__doc__),
    )
