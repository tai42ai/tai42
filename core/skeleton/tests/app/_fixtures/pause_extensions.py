"""Test-only extensions for the pause declaration: a plain wrapper and one registered ``pauses=True``."""

import functools

from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind


def _wrap(func, name: str, suffix: str):
    @functools.wraps(func)
    async def variant(*args, **kwargs):
        result = func(*args, **kwargs)
        if hasattr(result, "__await__"):
            result = await result
        return result

    variant.__name__ = f"{name}_{suffix}"
    variant.__qualname__ = variant.__name__
    return variant


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="plainwrap")
def plainwrap(func, name, desc):
    """A wrapper that never pauses."""
    return _wrap(func, name, "plainwrap")


@tai42_app.extensions.extension(kind=ExtensionKind.WRAPPER, name="relaywrap", pauses=True)
def relaywrap(func, name, desc):
    """A wrapper registered as pausing, whatever its base declares."""
    return _wrap(func, name, "relaywrap")
