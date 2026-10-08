"""The monitoring feature's shared writer logic: the encoder, references, and the OpenTelemetry writer.

Everything here except ``encode_payload`` is importable without the ``monitoring`` extra;
``encode_payload`` (orjson) is imported on first access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tai42_kit.monitoring.errors import MonitoringEncodeError
from tai42_kit.monitoring.refs import (
    PayloadRefValue,
    UnrecordedValue,
    escape_pointer_token,
    is_payload_ref,
    payload_ref,
    resolve_refs,
    unrecorded,
)

if TYPE_CHECKING:
    from tai42_kit.monitoring.encode import encode_payload

__all__ = [
    "MonitoringEncodeError",
    "PayloadRefValue",
    "UnrecordedValue",
    "encode_payload",
    "escape_pointer_token",
    "is_payload_ref",
    "payload_ref",
    "resolve_refs",
    "unrecorded",
]


def __getattr__(name: str) -> Any:
    if name == "encode_payload":
        from tai42_kit.monitoring.encode import encode_payload

        return encode_payload
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
