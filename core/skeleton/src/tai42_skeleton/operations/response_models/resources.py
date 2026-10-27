"""Response models for the resources operations."""

from __future__ import annotations

from pydantic import JsonValue, RootModel


class ResourceContent(RootModel[str | JsonValue]):
    """The loaded resource body of ``get_resource_by_id`` (both the GET fetch and the POST render route).

    The resource text, OR a media block. The fastmcp media members
    (``Image``/``Audio``/``File``) are not pydantic-v2 JSON-schema-able, so the
    media arm is described as its serialized JSON value (``JsonValue``) rather
    than a media model.
    """
