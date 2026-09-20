"""Cheap in-process isinstance checks on an inbound turn's optional params, form, attachments and location.

The doors validate the transport bounds before accept; these run only a defensive
isinstance sweep against the in-process caller so garbage never reaches a tool payload.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.conversations import validate_inbound_form
from tai42_contract.interactions import LocationElement, MediaItem, check_media_list


def _checked_params(params: dict[str, str] | None) -> dict[str, str] | None:
    """Refuse a non-dict or non-str-valued ``params`` loudly, keeping garbage out of the tool payload.

    The doors validate the bounds (``validate_entry_params``); accept trusts its
    in-process caller and runs only this cheap isinstance sweep. ``None`` passes through.
    """
    if params is None:
        return None
    if not isinstance(params, dict):
        raise ValueError(f"params must be a dict or None, got {type(params).__name__}")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    for key, value in params.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("params must map str keys to str values")  # noqa: TRY004 raised type is intentional (invariant/state/validation taxonomy); TypeError would change behaviour
    return params


def _checked_form(form: dict[str, Any] | None) -> dict[str, Any] | None:
    """Run the contract's inbound-form transport bounds defensively at the seam (the ``params`` pattern).

    A JSON object with string keys, finite numbers, bounded nesting and
    a bounded serialized size — the contents stay opaque and untrusted. The api door's
    ``ConversationMessage`` already validated its body; a channel adapter calling the facet
    directly is defended here just the same. ``None`` passes through; a violation raises
    ``ValueError`` BEFORE any state is written.
    """
    if form is None:
        return None
    return validate_inbound_form(form)


def _checked_attachments(attachments: list[MediaItem] | None) -> list[MediaItem] | None:
    """Run the shared list-level media caps defensively at the seam (the ``params`` pattern).

    ``None`` passes through; a present list is non-empty, within the item-count and summed-URI caps,
    each item already a validated ``MediaItem``. The api door's ``ConversationMessage`` validated
    its body; a channel adapter calling the facet directly is defended here just the same. A
    violation raises ``ValueError`` BEFORE any state is written.
    """
    if attachments is None:
        return None
    check_media_list(attachments)
    return attachments


def _checked_location(location: LocationElement | None) -> LocationElement | None:
    """Defensive seam guard for an inbound location.

    A ``LocationElement`` is self-validating (coordinate ranges, label bounds), so this only refuses a wrong TYPE
    loudly, keeping garbage out of the record and payload. ``None`` passes through.
    """
    if location is not None and not isinstance(location, LocationElement):
        raise ValueError(f"location must be a LocationElement or None, got {type(location).__name__}")
    return location
