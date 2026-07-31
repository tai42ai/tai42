"""Pure, dependency-free test helpers (imports only the stdlib, no vendor libs)."""

from __future__ import annotations

import typing

_PROTOCOL_SCAFFOLDING = {
    "_is_protocol",
    "_is_runtime_protocol",
    "__protocol_attrs__",
    "__init__",
    "__subclasshook__",
    "__class_getitem__",
}


def protocol_members(proto: type) -> set[str]:
    """Public members of a ``typing.Protocol`` (dunders + scaffolding stripped)."""
    members = set(typing.get_protocol_members(proto))
    return {m for m in members if m not in _PROTOCOL_SCAFFOLDING and not m.startswith("__")}
