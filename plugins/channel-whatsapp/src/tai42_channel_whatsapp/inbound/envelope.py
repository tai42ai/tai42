"""Webhook payload traversal: the ``entry[].changes[].value`` objects a POST batches."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)


def _iter_values(payload: Any) -> Iterator[dict[str, Any]]:
    """Every ``entry[].changes[].value`` object in a webhook payload.

    A webhook batches: multiple entries, changes, messages and statuses in one
    POST. Non-object ``entry``/``change`` items are skipped so a well-signed but
    odd payload never crashes the door.
    """
    if not isinstance(payload, dict):
        return
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning("whatsapp entry is not an object; skipping: %r", entry)
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                logger.warning("whatsapp change is not an object; skipping: %r", change)
                continue
            value = change.get("value")
            if isinstance(value, dict):
                yield value


def _as_list(value: Any) -> list[Any]:
    """The value if it is a JSON array, else an empty list."""
    return value if isinstance(value, list) else []
