"""Secrets contract: the generic secret-value envelope and its transforms.

``SecretValue`` wraps a real value so it never leaks through a repr, a log
line, or a JSON dump; ``reveal`` is the sole way out. ``mask_secrets`` and
``unwrap_secrets`` walk a JSON-shaped structure to replace wrapped values with
a placeholder or their revealed contents.
"""

from __future__ import annotations

from typing import Any

SECRET_PLACEHOLDER = "[secret]"


class SecretValue:
    """A wrapped value that resists accidental disclosure.

    ``reveal`` is the only path to the real value; ``repr``/``str`` yield the
    placeholder. Deliberately not JSON-serializable — an un-audited path that
    tries to dump it raises ``TypeError`` and fails loudly instead of leaking.
    Comparison/hashing use identity (inherited): value equality would invite
    timing and log traps.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        self._value = value

    def reveal(self) -> Any:
        return self._value

    def __repr__(self) -> str:
        return f"SecretValue({SECRET_PLACEHOLDER})"

    __str__ = __repr__


def mask_secrets(obj: Any) -> Any:
    """Deep copy ``obj`` with every ``SecretValue`` replaced by the placeholder.

    Dicts/lists/tuples are recursed (tuples return as lists); every other value
    passes through by reference. Never calls ``reveal``. A self-referential
    structure raises ``RecursionError`` — cycles are already invalid JSON.
    """
    return _walk(obj, lambda secret: SECRET_PLACEHOLDER)


def unwrap_secrets(obj: Any) -> Any:
    """Deep copy ``obj`` with every ``SecretValue`` replaced by its revealed value.

    Same walk and cycle contract as ``mask_secrets``.
    """
    return _walk(obj, lambda secret: secret.reveal())


def contains_secrets(obj: Any) -> bool:
    """Whether ``obj`` holds any ``SecretValue`` leaf.

    Recurses dict/list/tuple with the same walk contract as ``mask_secrets``;
    never calls ``reveal``. Short-circuits on the first secret found.
    """
    if isinstance(obj, SecretValue):
        return True
    if isinstance(obj, dict):
        return any(contains_secrets(value) for value in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(contains_secrets(item) for item in obj)
    return False


def _walk(obj: Any, on_secret: Any) -> Any:
    if isinstance(obj, SecretValue):
        return on_secret(obj)
    if isinstance(obj, dict):
        return {key: _walk(value, on_secret) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_walk(item, on_secret) for item in obj]
    return obj


__all__ = ["SECRET_PLACEHOLDER", "SecretValue", "contains_secrets", "mask_secrets", "unwrap_secrets"]
