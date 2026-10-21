"""A tool module under postponed annotations: ``keep``'s hints are the strings
``"dict[str, Any]"``, which only this module's globals resolve."""

from __future__ import annotations

from typing import Any


def keep(payload: dict[str, Any]) -> dict[str, Any]:
    return payload
