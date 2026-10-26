"""The ``Storage.list`` directory scope, for the in-memory storage fakes."""

from __future__ import annotations

from collections.abc import Iterable

from tai42_contract.storage import directory_prefix


def keys_under(keys: Iterable[str], directory: str | None) -> list[str]:
    """``keys`` as a list, or only those beneath ``directory`` when one is given, as a provider lists them."""
    if directory is None:
        return list(keys)
    prefix = directory_prefix(directory)
    return [key for key in keys if key.startswith(prefix)]
