"""Parsers that turn a webhook request body (JSON, XML, form, raw) into hook payload fields."""

import base64
import logging
from typing import Any

import xmltodict
from starlette.requests import Request

logger = logging.getLogger(__name__)


async def _parse_json_body(request: Request) -> dict[str, Any]:
    """Parse a JSON body into the payload fields it contributes.

    A JSON object merges whole; a valid non-object JSON body (array, scalar) is
    carried under ``body``. A malformed body raises ``ValueError``.
    """
    try:
        json_data = await request.json()
    except Exception as e:
        raise ValueError(f"malformed JSON body: {e}") from e
    if isinstance(json_data, dict):
        return json_data
    # A valid non-object JSON body (array, scalar) is carried whole.
    return {"body": json_data}


async def _parse_xml_body(request: Request) -> dict[str, Any]:
    """Parse an XML body into the payload fields it contributes.

    ``disable_entities=True`` turns off entity resolution so a hostile body
    (billion-laughs / entity-expansion) is rejected, not expanded unbounded. A
    malformed body raises ``ValueError``; an empty body yields ``{}``.
    """
    body_bytes = await request.body()
    if not body_bytes:
        return {}
    try:
        # xmltodict converts XML structure to a standard Python Dict.
        xml_data = xmltodict.parse(
            body_bytes,
            dict_constructor=dict,  # Ensure standard dicts, not OrderedDicts
            disable_entities=True,
        )
    except Exception as e:
        raise ValueError(f"malformed XML body: {e}") from e
    return xml_data


async def _parse_form_body(request: Request) -> dict[str, Any]:
    """Parse a urlencoded/multipart form body into a dict.

    A malformed body raises ``ValueError``.
    """
    try:
        form_data = await request.form()
    except Exception as e:
        raise ValueError(f"malformed form body: {e}") from e
    return dict(form_data)


async def _parse_raw_body(request: Request) -> dict[str, Any]:
    """Carry an untyped body whole: utf-8 text under ``raw_body``, else base64 under ``raw_body_base64``.

    An empty body yields ``{}``; the body is never dropped.
    """
    body_bytes = await request.body()
    if not body_bytes:
        return {}
    try:
        return {"raw_body": body_bytes.decode("utf-8")}
    except UnicodeDecodeError:
        return {"raw_body_base64": base64.b64encode(body_bytes).decode("ascii")}


async def parse_any_payload(request: Request, include_query: bool = True) -> dict[str, Any]:
    """Parse a webhook request into the payload dict hooks run on.

    A body that contradicts its declared Content-Type raises ``ValueError`` —
    firing hooks on a partially-salvaged payload would run flows on wrong
    data. Bodies with no declared type pass through explicitly: text as
    ``raw_body``, binary as ``raw_body_base64`` — never dropped.

    ``include_query=False`` drops the query string from the payload. A
    body-signature verifier authenticates the raw body only; folding the
    unauthenticated query string into the dispatched payload would let a captured
    signed delivery be replayed with attacker-appended ``?key=val`` params, so a
    caller that verified such a topic parses the body alone.
    """
    data: dict[str, Any] = {}

    if include_query and request.query_params:
        data.update(dict(request.query_params))

    content_type = request.headers.get("Content-Type", "").lower()

    if "application/json" in content_type:
        data.update(await _parse_json_body(request))
    elif "application/xml" in content_type or "text/xml" in content_type:
        data.update(await _parse_xml_body(request))
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        data.update(await _parse_form_body(request))
    else:
        data.update(await _parse_raw_body(request))

    return data
