r"""The one encoding of a recorded value: orjson, secrets masked, markers rendered and reserved.

``encode_payload`` runs on the caller's thread, once per recorded input/output/metadata
value. orjson encodes the JSON-native types (and dataclasses, datetimes, UUIDs, enums)
itself and calls ``_default`` for everything else.

The reserved marker namespace (``RESERVED_KEY_PREFIX``): a marker object always renders
with its one key first, as ``{"$tai42_…``; inside a JSON string that text is escaped
(``{\"$tai42_…``) and never matches. So the count of ``{"$tai42_`` in the encoding equals
the markers ``_default`` rendered exactly when the user data carries none; any surplus is a
user value carrying a marker-shaped object, refused loudly.
"""

from __future__ import annotations

import base64
import collections
import ipaddress
import pathlib
import re
from datetime import timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import orjson
from pydantic import BaseModel
from tai42_contract.monitoring import PAYLOAD_REF_KEY, RESERVED_KEY_PREFIX, UNRECORDED_KEY
from tai42_contract.secrets import SECRET_PLACEHOLDER, SecretValue

from tai42_kit.monitoring.errors import MonitoringEncodeError
from tai42_kit.monitoring.refs import PayloadRefValue, UnrecordedValue

__all__ = ["BYTES_KEY", "UNENCODABLE_KEY", "MonitoringEncodeError", "encode_payload"]

# The writer's byte-string form ``{"$tai42_bytes": <base64>}``.
BYTES_KEY = RESERVED_KEY_PREFIX + "bytes"
# The writer's form of a field it could not encode ``{"$tai42_unencodable": <type qualname>}``.
UNENCODABLE_KEY = RESERVED_KEY_PREFIX + "unencodable"

_MARKER_OPENING = '{"' + RESERVED_KEY_PREFIX
_IP_TYPES = (
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    ipaddress.IPv4Network,
    ipaddress.IPv6Network,
    ipaddress.IPv4Interface,
    ipaddress.IPv6Interface,
)


def encode_payload(value: Any) -> str:
    """Encode ``value`` as JSON text, masking every ``SecretValue`` and rendering the kit's markers.

    Raises ``MonitoringEncodeError`` for a value of a type with no monitoring form, an
    integer orjson cannot represent, or a user object whose first key starts with the
    reserved ``"$tai42_"`` prefix.
    """
    rendered = [0]
    try:
        text = orjson.dumps(value, default=partial(_default, rendered), option=orjson.OPT_NON_STR_KEYS).decode()
    except orjson.JSONEncodeError as exc:
        if isinstance(exc.__cause__, MonitoringEncodeError):
            raise exc.__cause__ from None
        raise MonitoringEncodeError(str(exc)) from exc
    if text.count(_MARKER_OPENING) != rendered[0]:
        raise MonitoringEncodeError(
            f'value carries an object whose first key starts with the reserved prefix "{RESERVED_KEY_PREFIX}"; '
            "monitoring markers are written by the writer only"
        )
    return text


def _default(rendered: list[int], obj: Any) -> Any:  # noqa: C901 - one flat type dispatch
    if isinstance(obj, PayloadRefValue):
        rendered[0] += 1
        return {PAYLOAD_REF_KEY: obj.ref.model_dump(exclude_none=True)}
    if isinstance(obj, UnrecordedValue):
        rendered[0] += 1
        return {UNRECORDED_KEY: True}
    if isinstance(obj, SecretValue):
        return SECRET_PLACEHOLDER
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="python")
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, timedelta):
        return obj.total_seconds()
    if isinstance(obj, (set, frozenset, collections.deque)):
        return list(obj)  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(obj, (bytes, bytearray)):
        rendered[0] += 1
        return {BYTES_KEY: base64.b64encode(obj).decode()}
    if isinstance(obj, BaseException):
        return {"error_type": type(obj).__qualname__, "message": str(obj)}
    if isinstance(obj, pathlib.PurePath):
        return str(obj)
    if isinstance(obj, re.Pattern):
        return obj.pattern  # pyright: ignore[reportUnknownMemberType]
    if isinstance(obj, _IP_TYPES):
        return str(obj)
    raise MonitoringEncodeError(f"cannot encode a value of type {type(obj).__qualname__} for monitoring")
