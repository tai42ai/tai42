"""Reduces a tool-run return (``ToolResult`` or raw value) to the JSON-able value
each door expects, keeping ``SecretValue`` wrapped."""

from typing import Any

from fastmcp.utilities.types import Audio, File, Image
from pydantic_core import PydanticSerializationError, to_jsonable_python
from tai42_contract.interactions import SuspendedInteraction
from tai42_contract.secrets import SecretValue


def _tool_result_value(result: Any) -> Any:
    """Reduce a ``ToolResult`` (a transformed tool's ``run`` output) to the same
    raw, JSON-able value the callable run path returns.

    ``structured_content`` is the structured form of the tool's return; for a
    non-object return FastMCP WRAPS it as ``{"result": <value>}`` and flags the
    wrap on ``_meta.fastmcp.wrap_result`` — unwrap that so a scalar/string preset
    returns its bare value, exactly as a direct call of the base tool would. With
    no structured content, fall back to the text blocks; a media return
    (Image/Audio/File) carries no structured and no text, so serialize its
    remaining content blocks to their JSON wire dicts — the same media shape the
    direct-run path preserves. Only a genuinely empty result reduces to ``None``."""
    structured = result.structured_content
    meta = result.meta or {}
    if isinstance(structured, dict) and meta.get("fastmcp", {}).get("wrap_result"):
        return to_jsonable_python(structured["result"])
    if structured is not None:
        return to_jsonable_python(structured)
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    if texts:
        return texts[0] if len(texts) == 1 else texts
    non_text = [to_jsonable_python(block) for block in result.content if getattr(block, "type", None) != "text"]
    if not non_text:
        return None
    return non_text[0] if len(non_text) == 1 else non_text


def _jsonable_keeping_secrets(value: Any) -> Any:
    """JSON-normalize ``value`` but keep any ``SecretValue`` wrapped.

    The shared in-process seam keeps the wrapper so each door decides: the sync
    run-tool door reveals it, the tool-run recorder masks it. Recursion mirrors the
    ``mask``/``unwrap`` walk (dict/list/tuple); every OTHER non-JSON-native leaf
    still raises loudly through ``to_jsonable_python``."""
    if isinstance(value, SecretValue):
        return value
    if isinstance(value, dict):
        return {key: _jsonable_keeping_secrets(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_keeping_secrets(item) for item in value]
    return to_jsonable_python(value)


def _serialize_result(result: Any) -> Any:
    """Reduce a direct tool-run return to a JSON-native value.

    A live fastmcp media object (``Image`` / ``Audio`` / ``File``) is NOT
    serializable by ``to_jsonable_python`` (it raises) — fastmcp's own
    media-to-MCP-content conversion lives only in ``Tool.run``, which the direct
    run path bypasses. Convert it to its MCP content first: ``Image`` / ``Audio``
    become the media wire dict ``{"type": "image"|"audio", "data": <b64>,
    "mimeType": <mime>}``; a ``File`` becomes an ``EmbeddedResource``
    (``{"type": "resource", ...}``) — JSON-native, but NOT the media wire shape,
    so the direct-run UI renders it as JSON rather than as media. Any other
    result serializes directly via ``to_jsonable_python``.

    A ``SecretValue`` is deliberately not JSON-serializable, so a result carrying
    one keeps the wrapper through the seam (revealed at the sync door, masked by the
    recorder); every other unserializable type still raises loudly."""
    if isinstance(result, SuspendedInteraction):
        # An async ask_user parks the caller and returns this sentinel; keep the
        # object through the direct-run seam (never flattened to a plain dict) so the
        # turn engine recognizes the park by TYPE and ends the turn silently. Each
        # edge door that serializes for the wire does so at its own boundary.
        return result
    if isinstance(result, Image):
        result = result.to_image_content()
    elif isinstance(result, Audio):
        result = result.to_audio_content()
    elif isinstance(result, File):
        result = result.to_resource_content()
    try:
        return to_jsonable_python(result)
    except PydanticSerializationError:
        return _jsonable_keeping_secrets(result)
