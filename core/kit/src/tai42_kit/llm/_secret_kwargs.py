"""Shared helpers for the LLM/embedding builders.

The builders cache one instance per ``(provider, kwargs)`` pair. Secret settings
(e.g. a ``SecretStr`` ``api_key`` arriving via ``model_dump -> **kwargs``) must
never appear in the cache key in plaintext, yet must reach the provider
constructor as plain strings. ``KwargsCacheKey`` keys the cache on a canonical
JSON string in which every ``SecretStr`` value is replaced by the SHA-256 digest
of its plaintext, while carrying the original kwargs (secrets still wrapped) for
the constructor; ``unwrap_secret_kwargs`` reveals the secrets at that seam only.
"""

import hashlib
import json
from typing import Any

from pydantic import SecretStr


def unwrap_secret_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Reveal every ``SecretStr`` plaintext, recursing into nested containers.

    A ``SecretStr`` nested inside a dict/list/tuple kwarg is unwrapped too, so
    the provider constructor receives plain strings wherever a secret sits, not
    just at the top level.
    """
    return {k: _unwrap_secrets(v) for k, v in kwargs.items()}


def _unwrap_secrets(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {k: _unwrap_secrets(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(_unwrap_secrets(v) for v in value)
    if isinstance(value, list):
        return [_unwrap_secrets(v) for v in value]
    return value


def _mask_secrets(value: Any) -> Any:
    """Replace every ``SecretStr`` with ``sha256:<digest>`` for cache keying.

    Recurses into nested dict/list/tuple containers so a secret at any depth is
    keyed by a stable, non-reversible digest of its plaintext — equal secrets
    collapse to one cache entry while no plaintext (and no value-blind
    ``SecretStr`` repr, which would collide distinct secrets) reaches the key.
    """
    if isinstance(value, SecretStr):
        return f"sha256:{_digest(value)}"
    if isinstance(value, dict):
        return {k: _mask_secrets(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_secrets(v) for v in value]
    return value


def _digest(secret: SecretStr) -> str:
    return hashlib.sha256(secret.get_secret_value().encode("utf-8")).hexdigest()


class KwargsCacheKey:
    """Hashable ``lru_cache`` key wrapping builder kwargs.

    Hash and equality use a canonical JSON string in which each ``SecretStr``
    value — at any nesting depth — is replaced by ``sha256:<digest of its
    plaintext>``, so equal secrets share a cache entry while no plaintext ever
    enters the key. ``kwargs`` holds the caller's original mapping untouched
    (tuples stay tuples, secrets stay wrapped) for the provider constructor.
    """

    __slots__ = ("_key", "kwargs")

    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        # ``default=repr`` fingerprints any non-serializable leaf (e.g. an
        # embedding object) rather than crashing, matching store_registry's key.
        self._key = json.dumps(_mask_secrets(kwargs), sort_keys=True, default=repr)

    def __hash__(self) -> int:
        return hash(self._key)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, KwargsCacheKey) and self._key == other._key
