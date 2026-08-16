"""Secrets contract: the generic secret-value envelope and its transforms.

``SecretValue`` wraps a real value so it never leaks through a repr, a log
line, or a JSON dump; ``reveal`` is the sole way out. ``mask_secrets`` and
``unwrap_secrets`` walk a JSON-shaped structure to replace wrapped values with
a placeholder or their revealed contents.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

SECRET_PLACEHOLDER = "[secret]"


class SecretValue:
    """A wrapped value that resists accidental disclosure.

    ``reveal`` is the only path to the real value; ``repr``/``str`` yield the
    placeholder. The envelope refuses every generic transport by design — json
    (no serializer) AND pickle/copy/deepcopy (``__reduce__`` raises): an
    un-audited path that tries to move it raises ``TypeError`` and fails loudly
    instead of leaking. Comparison/hashing use identity (inherited): value
    equality would invite timing and log traps.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        self._value = value

    def reveal(self) -> Any:
        return self._value

    def __reduce__(self) -> Any:
        # The reduce protocol backs pickle, copy, and deepcopy alike; refusing
        # it here blocks every generic serialization transport at one hook.
        raise TypeError("SecretValue refuses serialization transport")

    def __repr__(self) -> str:
        return f"SecretValue({SECRET_PLACEHOLDER})"

    __str__ = __repr__


def mask_secrets(obj: Any) -> Any:
    """Deep copy ``obj`` with every ``SecretValue`` replaced by the placeholder.

    Dicts/lists/tuples are recursed (tuples return as lists); every other value
    passes through by reference. Never calls ``reveal``. A self-referential
    structure raises ``RecursionError`` — cycles are already invalid JSON.
    """
    return _walk(obj, _to_placeholder)


def unwrap_secrets(obj: Any) -> Any:
    """Deep copy ``obj`` with every ``SecretValue`` replaced by its revealed value.

    Same walk and cycle contract as ``mask_secrets``.
    """
    return _walk(obj, _to_revealed)


def contains_secrets(obj: Any) -> bool:
    """Whether ``obj`` holds any ``SecretValue`` leaf.

    Recurses dict/list/tuple with the same walk contract as ``mask_secrets``;
    never calls ``reveal``. Short-circuits on the first secret found.
    """
    if isinstance(obj, SecretValue):
        return True
    if isinstance(obj, dict):
        mapping = cast("dict[Any, Any]", obj)
        return any(contains_secrets(value) for value in mapping.values())
    if isinstance(obj, (list, tuple)):
        sequence = cast("list[Any] | tuple[Any, ...]", obj)
        return any(contains_secrets(item) for item in sequence)
    return False


def _to_placeholder(secret: SecretValue) -> str:
    return SECRET_PLACEHOLDER


def _to_revealed(secret: SecretValue) -> Any:
    return secret.reveal()


def _walk(obj: Any, on_secret: Callable[[SecretValue], Any]) -> Any:
    if isinstance(obj, SecretValue):
        return on_secret(obj)
    if isinstance(obj, dict):
        mapping = cast("dict[Any, Any]", obj)
        return {key: _walk(value, on_secret) for key, value in mapping.items()}
    if isinstance(obj, (list, tuple)):
        sequence = cast("list[Any] | tuple[Any, ...]", obj)
        return [_walk(item, on_secret) for item in sequence]
    return obj


__all__ = ["SECRET_PLACEHOLDER", "SecretValue", "contains_secrets", "mask_secrets", "unwrap_secrets"]
