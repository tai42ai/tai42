"""The ``cache`` tool extension (WRAPPER kind).

Branches a tool into a ``<tool>_cache`` variant that memoizes results by call arguments, adding
one reserved control kwarg ``exp`` (seconds-to-live) so the branch stays schema-preserving.
Concurrent identical calls single-flight on a per-key lock. The store is process-local and the
extension does NOT set ``requires_body_locality``: under an execution-relocating extension each
worker holds its own store, so a cross-worker call is a miss — a hit-rate cost, never a wrong result.
"""

import inspect

from makefun import create_function
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind
from tai42_kit.utils.data import makefun_func_name

from tai42_toolbox._internal.extensions.cache_store import MISS, CacheStore, compute_key
from tai42_toolbox._internal.extensions.signature import with_added_params


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="cache")
def cache(func, name, description):
    """Branch ``func`` into a memoizing ``<name>_cache`` variant."""
    store = CacheStore()

    async def wrapper(*args, **kwargs):
        exp = kwargs.pop("exp", None)
        key = compute_key(*args, **kwargs)

        # 1. Fast-path read (no lock).
        value = store.read(key)
        if value is not MISS:
            return value

        # 2. Single-flight: the first caller computes and writes; concurrent callers for the
        # same key wait on the lock, then read the written value instead of re-executing.
        lock = store.key_lock(key)
        try:
            async with lock:
                value = store.read(key)
                if value is not MISS:
                    return value

                if inspect.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)

                store.write(key, result, exp)
                return result
        finally:
            store.drop_lock(key)

    # Present the wrapped tool's signature plus the reserved ``exp`` control kwarg.
    sig = inspect.signature(func)
    exp_param = inspect.Parameter("exp", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=float | None)
    new_sig = with_added_params(sig, exp_param)

    raw_name = f"{name}_{cache.__name__}"
    safe_name = makefun_func_name(raw_name)

    branch = create_function(
        func_signature=new_sig,
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


# The apply site subtracts these reserved kwargs from the branch's input schema before checking
# a WRAPPER preserves the wrapped tool's schema. The attribute rides on the factory.
cache.reserved_params = frozenset({"exp"})
