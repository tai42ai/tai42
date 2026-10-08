"""The monitoring feature's shared logic: the encoder, references, the OpenTelemetry writer, and the row preview.

Everything here except ``encode_payload`` is importable without the ``monitoring`` extra;
``encode_payload`` (orjson) is imported on first access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tai42_kit.monitoring.errors import MonitoringEncodeError
from tai42_kit.monitoring.preview import TRACE_PREVIEW_MAX_CHARS, preview
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
    "TRACE_PREVIEW_MAX_CHARS",
    "MonitoringEncodeError",
    "PayloadRefValue",
    "UnrecordedValue",
    "encode_payload",
    "escape_pointer_token",
    "is_payload_ref",
    "payload_ref",
    "preview",
    "resolve_refs",
    "unrecorded",
]


def __getattr__(name: str) -> Any:
    if name == "encode_payload":
        from tai42_kit.monitoring.encode import encode_payload

        return encode_payload
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
