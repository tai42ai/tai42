"""The listing filter the storage and template list operations share."""

from __future__ import annotations

from collections.abc import Iterable

from tai42_contract.app import tai42_app

from tai42_skeleton.storage.prefix_marks import covering_prefix


def listed_keys(keys: Iterable[str]) -> list[str]:
    """Return ``keys`` in order, without those under a prefix the live epoch marks unlisted."""
    unlisted = tai42_app.storage.unlisted_prefixes()
    return [key for key in keys if covering_prefix(key, unlisted) is None]
