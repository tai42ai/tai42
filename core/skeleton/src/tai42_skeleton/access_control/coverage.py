"""The one scope-coverage rule every access-control door applies to a resolved resource-id set."""

from __future__ import annotations

from collections.abc import Collection, Iterable

from tai42_contract.access_control import UNIVERSAL_SCOPE


def is_public_only(resource_ids: Collection[str], public_id: str) -> bool:
    """Whether the resolved ids are the public id alone (deny wins: any protected id makes it protected)."""
    return bool(resource_ids) and set(resource_ids) == {public_id}


def scopes_cover(resource_ids: Iterable[str], scopes: Collection[str], public_id: str) -> bool:
    """Whether ``scopes`` cover EVERY protected id in ``resource_ids`` (the public id needs no scope)."""
    return UNIVERSAL_SCOPE in scopes or all(rid in scopes for rid in resource_ids if rid != public_id)
