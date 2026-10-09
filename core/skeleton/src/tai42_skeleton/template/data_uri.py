"""Inline ``data:`` URIs and the image-only guard of the template media boundary."""

import base64
from urllib.parse import unquote_to_bytes


def decode_data_uri(uri: str) -> tuple[bytes, str | None]:
    """Decode a ``data:`` URI into ``(bytes, mime)``.

    Parses the inline ``data:[<mediatype>][;base64],<payload>`` form — no network
    or storage access. A URI without the required comma separator is malformed and
    raises loudly.
    """
    header, sep, payload = uri[len("data:") :].partition(",")
    if not sep:
        raise ValueError(f"Malformed data URI (missing ','): {uri[:64]!r}")
    is_base64 = header.endswith(";base64")
    mediatype = header[: -len(";base64")] if is_base64 else header
    data = base64.b64decode(payload) if is_base64 else unquote_to_bytes(payload)
    mime = mediatype.split(";", 1)[0] or None
    return data, mime


def data_uri(data: bytes, mime: str) -> str:
    """Build a base64 ``data:`` URI that always carries a resolved ``mime``."""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def assert_image(mime: str | None, source: object) -> None:
    """Guard the image-only media boundary: a resolved ``image/*`` mime is required."""
    if mime is None or not mime.startswith("image/"):
        raise ValueError(f"normalize_media requires an image resource; resolved mime {mime!r} for {source!r}")
