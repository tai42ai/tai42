"""Reduce a tool-run return (``ToolResult`` or raw value) to the JSON-able value each door expects.

Keeps ``SecretValue`` wrapped.
"""

from typing import Any

from fastmcp.utilities.types import Audio, File, Image
from pydantic_core import PydanticSerializationError, to_jsonable_python
from tai42_contract.interactions import ResumeBuffered, SuspendedInteraction
from tai42_contract.secrets import SecretValue

# The half-open UTF-16 surrogate range. A Python ``str`` may hold a code point here
# (e.g. ``"\ud83d"``); ``to_jsonable_python`` passes it through, but the final wire
# encode ``str.encode("utf-8")`` raises ``UnicodeEncodeError: surrogates not allowed``.
_SURROGATE_FLOOR = 0xD800
_SURROGATE_CEILING = 0xE000


class ToolResultEncodingError(Exception):
    """A tool's reduced result holds a lone UTF-16 surrogate no JSON encoder can render.

    Carries the ``tool_name`` that produced it and the ``json_path`` of the offending
    string leaf, so each door refuses loudly — naming the tool and the location — without
    ever putting the un-encodable value into the error (which would itself fail to encode).
    A standalone exception rather than an ``OperationError``: the ``tools/binding`` layer
    does not import ``operations``; each door maps it to its own edge error.
    """

    def __init__(self, tool_name: str, json_path: str) -> None:
        """Record the ``tool_name`` and the ``json_path`` of the un-encodable string leaf."""
        super().__init__(f"tool {tool_name!r} produced a result that cannot be JSON-encoded at {json_path}")
        self.tool_name = tool_name
        self.json_path = json_path


class UnencodableLeafError(Exception):
    """Internal signal: a leaf ``to_jsonable_python`` cannot render, carrying the leaf's JSON path.

    Raised inside the fallback walk when a leaf (a pydantic model / dataclass holding a lone
    surrogate in a dict key it owns, or another member JSON cannot encode) fails to reduce —
    WITHOUT deep-reducing its internals. Never leaves ``result.py`` as itself: each reduction
    point catches it and re-raises the door-appropriate named error (``ToolResultEncodingError``
    / edge error) carrying the same path, so a bad leaf becomes the named refusal, never a 500.
    """

    def __init__(self, path: str) -> None:
        """Record the JSON ``path`` of the leaf that could not be reduced."""
        super().__init__(f"result leaf at {path} cannot be JSON-encoded")
        self.path = path


def _holds_lone_surrogate(text: str) -> bool:
    """Whether ``text`` holds a code point in the UTF-16 surrogate range (first hit short-circuits)."""
    return any(_SURROGATE_FLOOR <= ord(char) < _SURROGATE_CEILING for char in text)


def _child_path(path: str, key: Any) -> str:
    """The path segment for a dict ``key``, ASCII-safe: a surrogate-bearing key is escaped via ``!a``.

    A surrogate in a key can never appear raw in a path (the path is itself JSON-encoded in the
    error), so it is escaped; a clean key (or a non-str key the JSON encoder coerces) appears
    verbatim.
    """
    if isinstance(key, str) and _holds_lone_surrogate(key):
        return f"{path}.{key!a}"
    return f"{path}.{key}"


def find_lone_surrogate(value: Any, path: str = "$") -> str | None:
    """The JSON path of the first ``str`` KEY or leaf holding a lone UTF-16 surrogate, else ``None``.

    A single bounded DFS over a reduced JSON-native ``value`` in the same order the
    ``json.dumps`` that follows would visit it — a dict recurses each entry KEY then value
    (``path`` gains ``.<key>``), a list/tuple recurses by index (``path`` gains ``[<i>]``), a
    ``str`` leaf is scanned once and short-circuits on the first surrogate, and a
    ``SecretValue`` leaf is revealed and its content recursed so a surrogate a door reveals
    later (the sync door's ``unwrap_secrets`` before the wire encode) is caught by the same
    walk. A dict KEY holding a surrogate is un-encodable exactly as a value is, so it is
    flagged too; it is named ASCII-safely via the ``!a`` conversion (which escapes the
    surrogate to an ASCII form) so the returned path never carries the offending code point
    into the error's OWN encoding. Every other path segment is an ancestor key or index, so the path
    names the offending FIELD, never the value, and is itself safely encodable. Any other
    leaf is JSON-native and cannot hold a surrogate, so it returns ``None``.
    """
    if isinstance(value, SecretValue):
        return find_lone_surrogate(value.reveal(), path)
    if isinstance(value, str):
        return path if _holds_lone_surrogate(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            # A str KEY holding a surrogate is itself un-encodable; it is named ASCII-safely
            # (``_child_path``) so the path never carries the offending code point. A non-str
            # key is coerced to a clean string by the JSON encoder and can hold no surrogate.
            if isinstance(key, str) and _holds_lone_surrogate(key):
                return _child_path(path, key)
            hit = find_lone_surrogate(item, _child_path(path, key))
            if hit is not None:
                return hit
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            hit = find_lone_surrogate(item, f"{path}[{index}]")
            if hit is not None:
                return hit
        return None
    return None


def _tool_result_value(result: Any) -> Any:
    """Reduce a ``ToolResult`` to the same raw, JSON-able value the callable run path returns.

    A ``ToolResult`` is a transformed tool's ``run`` output.
    ``structured_content`` is the structured form of the tool's return; for a
    non-object return FastMCP WRAPS it as ``{"result": <value>}`` and flags the
    wrap on ``_meta.fastmcp.wrap_result`` — unwrap that so a scalar/string preset
    returns its bare value, exactly as a direct call of the base tool would. With
    no structured content, fall back to the text blocks; a media return
    (Image/Audio/File) carries no structured and no text, so serialize its
    remaining content blocks to their JSON wire dicts — the same media shape the
    direct-run path preserves. Only a genuinely empty result reduces to ``None``.
    """
    structured = result.structured_content
    meta = result.meta or {}
    if isinstance(structured, dict) and meta.get("fastmcp", {}).get("wrap_result"):
        return _jsonable_or_keep_walkable(structured["result"])
    if structured is not None:
        return _jsonable_or_keep_walkable(structured)
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    if texts:
        return texts[0] if len(texts) == 1 else texts
    non_text = [to_jsonable_python(block) for block in result.content if getattr(block, "type", None) != "text"]
    if not non_text:
        return None
    return non_text[0] if len(non_text) == 1 else non_text


def _jsonable_keeping_secrets(value: Any, path: str = "$") -> Any:
    """JSON-normalize ``value`` but keep any ``SecretValue`` wrapped, tracking each leaf's path.

    The shared in-process seam keeps the wrapper so each door decides: the sync run-tool door
    reveals it, the tool-run recorder masks it. Recursion mirrors the ``mask``/``unwrap`` walk
    (dict/list/tuple), building the JSON ``path`` as it descends. A non-container, non-secret
    leaf ``to_jsonable_python`` cannot render — a pydantic model / dataclass holding a lone
    surrogate in a dict key it owns, or another unserializable member — raises
    :class:`UnencodableLeafError` carrying that leaf's path (WITHOUT deep-reducing the leaf), so
    the reduction point names it as the tool's un-encodable output rather than the walk failing
    anonymously into a 500.
    """
    if isinstance(value, SecretValue):
        return value
    if isinstance(value, dict):
        return {key: _jsonable_keeping_secrets(item, _child_path(path, key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_keeping_secrets(item, f"{path}[{index}]") for index, item in enumerate(value)]
    try:
        return to_jsonable_python(value)
    except (PydanticSerializationError, UnicodeEncodeError) as exc:
        raise UnencodableLeafError(path) from exc


def _jsonable_or_keep_walkable(value: Any) -> Any:
    """Reduce ``value`` to JSON-native, keeping a walkable structure when the encoder refuses.

    ``to_jsonable_python`` raises on a ``SecretValue`` (``PydanticSerializationError``) and on a
    lone surrogate in a dict KEY (``UnicodeEncodeError`` — unlike a surrogate in a VALUE, which it
    passes straight through). In both cases fall back to the manual walk, which keeps
    ``SecretValue`` wrapped and PRESERVES a surrogate key or value rather than dropping or
    escaping it — so the shared detector at the seam names it and each door refuses loudly rather
    than the reduction failing anonymously. A leaf the walk itself cannot render raises
    :class:`UnencodableLeafError` (carrying its path) for the reduction point to name.
    """
    try:
        return to_jsonable_python(value)
    except (PydanticSerializationError, UnicodeEncodeError):
        return _jsonable_keeping_secrets(value)


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
    recorder). A lone surrogate (key or value) is likewise kept walkable rather than
    raising here, so the seam's detector names it; every other unserializable type still
    raises loudly.
    """
    if isinstance(result, (SuspendedInteraction, ResumeBuffered)):
        # An async ask parks the caller and returns a ``SuspendedInteraction`` sentinel; a resume
        # that leaves sibling asks of the same super-step still open returns a ``ResumeBuffered``.
        # Both are non-terminal park signals: keep the object through the direct-run seam (never
        # flattened to a plain dict) so the delivery chokepoint and the turn engine recognize the
        # still-parked run by TYPE rather than mistaking it for a terminal result. Each edge door
        # that serializes for the wire does so at its own boundary.
        return result
    if isinstance(result, Image):
        result = result.to_image_content()
    elif isinstance(result, Audio):
        result = result.to_audio_content()
    elif isinstance(result, File):
        result = result.to_resource_content()
    return _jsonable_or_keep_walkable(result)
