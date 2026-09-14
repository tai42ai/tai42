"""Reconstructs a transformed tool's typed callable with its hidden baked args
applied, epoch-scoped LRU cached."""

import inspect
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, cast

from fastmcp.tools.function_tool import FunctionTool
from fastmcp.tools.tool_transform import TransformedTool
from makefun import create_function
from tai42_kit.utils.data import makefun_func_name

from tai42_skeleton.tools.binding.schema import _resolved_signature


def _baked_partial(tool_obj: TransformedTool) -> Callable[..., Any]:
    """A typed partial of a transformed tool's underlying function with its hidden
    baked args applied — the callable an extension branch wraps.

    The remaining parameters keep the underlying function's real typed signature;
    a call that passes a baked key is rejected (the branch may not re-open a baked
    constant), matching the bound tool's own contract. A preset only ever bakes
    ``ArgTransform(hide=True, default=<value>)``, so a non-hidden transform arg has
    no branch-composable meaning and raises loudly rather than mis-binding."""
    parent = tool_obj.parent_tool
    if not isinstance(parent, FunctionTool):
        raise TypeError(f"transformed tool {tool_obj.name!r} has no callable base to branch")
    base_fn = parent.fn

    baked: dict[str, Any] = {}
    for key, transform in tool_obj.transform_args.items():
        if transform.hide is not True:
            raise TypeError(
                f"cannot branch-bind transform arg {key!r} of {tool_obj.name!r}: only hidden baked args are supported"
            )
        baked[key] = transform.default

    signature = _resolved_signature(base_fn)
    remaining = [p for p in signature.parameters.values() if p.name not in baked]
    presented = signature.replace(parameters=remaining)

    def _reject_baked(kwargs: dict[str, Any]) -> None:
        clashing = [key for key in baked if key in kwargs]
        if clashing:
            raise TypeError(f"{tool_obj.name!r} does not accept baked argument(s): {', '.join(sorted(clashing))}")

    def _forward_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        # Bind positionals through the PRESENTED signature (which excludes the
        # baked params), so a positional call maps each value to the remaining
        # param it actually names — never onto a hidden baked slot. The baked
        # constants are then merged in by name.
        _reject_baked(kwargs)
        bound = presented.bind(*args, **kwargs)
        return {**baked, **bound.arguments}

    if inspect.iscoroutinefunction(base_fn):

        async def _impl_async(*args, **kwargs):
            return await base_fn(**_forward_args(args, kwargs))

        impl: Callable[..., Any] = _impl_async
    else:

        def _impl_sync(*args, **kwargs):
            return base_fn(**_forward_args(args, kwargs))

        impl = _impl_sync

    # makefun's ``doc`` is annotated ``str`` but accepts ``None`` (its own default),
    # falling back to the impl's docstring.
    return create_function(
        presented,
        cast(Callable[[Any], Any], impl),
        func_name=makefun_func_name(tool_obj.name),
        doc=cast(str, base_fn.__doc__),
    )


# A preset's makefun-compiled partial per resolved tool object, so a repeated live resolve
# yields the SAME callable and fastmcp's identity-keyed ``without_injected_parameters`` LRU
# still hits. Keyed on ``id`` (a fastmcp ``Tool`` is unhashable): safe only because the cached
# partial strongly references its source tool, pinning that id for the entry's life.
#
# EPOCH-SCOPED: the strong reference would otherwise pin a retired epoch's tools (and their
# id slots, an id-reuse hazard) for the process lifetime. The cache is dropped the moment the
# serving generation advances, so it only ever holds the LIVE epoch's tools.
_BAKED_PARTIAL_CACHE_MAX = 2048
_baked_partial_cache: "OrderedDict[int, Callable[..., Any]]" = OrderedDict()
_baked_partial_cache_epoch: int | None = None


def _cached_baked_partial(tool_obj: TransformedTool) -> Callable[..., Any]:
    from tai42_skeleton.app.epoch import current_epoch_or_none

    global _baked_partial_cache_epoch
    epoch = current_epoch_or_none()
    number = epoch.number if epoch is not None else None
    if number != _baked_partial_cache_epoch:
        # A new serving generation: drop the retired epoch's pinned partials.
        _baked_partial_cache.clear()
        _baked_partial_cache_epoch = number

    key = id(tool_obj)
    cached = _baked_partial_cache.get(key)
    if cached is not None:
        _baked_partial_cache.move_to_end(key)
        return cached
    partial = _baked_partial(tool_obj)
    _baked_partial_cache[key] = partial
    if len(_baked_partial_cache) > _BAKED_PARTIAL_CACHE_MAX:
        _baked_partial_cache.popitem(last=False)
    return partial
