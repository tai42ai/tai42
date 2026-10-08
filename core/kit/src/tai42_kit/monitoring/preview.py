"""The structurally-bounded preview of a value, for list rows that show a trace's input/output.

``preview`` keeps a row payload small while staying valid JSON: it clips at the
structure level (string leaves, entries per object/array, nesting depth), never
mid-string, so a client always renders the preview as a parsed tree.
"""

from __future__ import annotations

import ast
import json
from typing import Any, cast

from pydantic import JsonValue

__all__ = ["TRACE_PREVIEW_MAX_CHARS", "preview"]

# The cut is explicit (a terminal marker at each clip), never a silent truncation of a complete value.
TRACE_PREVIEW_MAX_CHARS = 512  # max chars per string leaf
_PREVIEW_ITEMS = 20  # max entries per object / array
_PREVIEW_DEPTH = 6  # max nesting depth
_PREVIEW_PARSE_MAX = 20_000  # never structurally parse a string larger than this


def _maybe_json(text: str) -> Any:
    """Parse a string that is itself an object/array, else return ``None``.

    JSON is tried first, then a Python ``repr`` (single quotes, ``True``/``False``/``None``) via
    ``literal_eval``. Lets a stringified structure be bounded structurally (and rendered as a tree)
    instead of char-clipped into garbage. A string over ``_PREVIEW_PARSE_MAX`` is not parsed — it would
    be fully materialized just to keep a few items — so the caller char-clips it instead.
    """
    s = text.strip()
    if len(s) < 2 or len(s) > _PREVIEW_PARSE_MAX or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except ValueError:
        pass
    try:
        # literal_eval is safe — only Python literals, no code execution.
        return ast.literal_eval(s)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None


def _preview_str(value: str, depth: int) -> JsonValue:
    # A stringified structure is parsed and bounded as a tree; a plain/non-JSON string leaf
    # (dates, etc.) is char-clipped at ``TRACE_PREVIEW_MAX_CHARS``.
    parsed = _maybe_json(value)
    if isinstance(parsed, (dict, list)):
        return preview(parsed, depth)
    return value if len(value) <= TRACE_PREVIEW_MAX_CHARS else value[:TRACE_PREVIEW_MAX_CHARS] + "…"


def _preview_mapping(obj: dict[Any, Any], depth: int) -> JsonValue:
    # An object capped at ``_PREVIEW_ITEMS`` entries with a per-item recurse; nesting past
    # ``_PREVIEW_DEPTH`` is elided to a terminal marker.
    if depth >= _PREVIEW_DEPTH:
        return {"…": "…"}
    out: dict[str, JsonValue] = {}
    for i, (k, v) in enumerate(obj.items()):
        if i >= _PREVIEW_ITEMS:
            out["…"] = f"+{len(obj) - _PREVIEW_ITEMS} more"
            break
        out[str(k)] = preview(v, depth + 1)
    return out


def _preview_sequence(seq: list[Any] | tuple[Any, ...], depth: int) -> JsonValue:
    # An array capped at ``_PREVIEW_ITEMS`` entries with a per-item recurse; nesting past
    # ``_PREVIEW_DEPTH`` is elided to a terminal marker.
    if depth >= _PREVIEW_DEPTH:
        return ["…"]
    items: list[JsonValue] = [preview(v, depth + 1) for v in seq[:_PREVIEW_ITEMS]]
    if len(seq) > _PREVIEW_ITEMS:
        items.append(f"+{len(seq) - _PREVIEW_ITEMS} more")
    return items


def _preview_scalar(value: Any) -> JsonValue:
    # A number/bool/None passthrough; anything else (dates, etc.) is a str-fallback clip.
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)  # dates, etc. — JSON-safe fallback
    return text if len(text) <= TRACE_PREVIEW_MAX_CHARS else text[:TRACE_PREVIEW_MAX_CHARS] + "…"


def preview(value: Any, depth: int = 0) -> JsonValue:
    """Build a structurally-bounded run-list preview of a trace's input/output.

    String leaves cut to ``TRACE_PREVIEW_MAX_CHARS``, objects/arrays capped at ``_PREVIEW_ITEMS`` entries,
    nesting elided past ``_PREVIEW_DEPTH`` — each with a terminal marker — so the result is small but still
    valid JSON the client renders as a tree. ``None`` stays ``None``; a stringified structure is parsed and
    bounded, a plain string or non-JSON leaf (dates, etc.) char-clipped. The cut is by design — the full
    value lives on ``get_trace``.
    """
    if isinstance(value, str):
        return _preview_str(value, depth)
    if isinstance(value, dict):
        return _preview_mapping(cast("dict[Any, Any]", value), depth)
    if isinstance(value, (list, tuple)):
        return _preview_sequence(cast("list[Any] | tuple[Any, ...]", value), depth)
    return _preview_scalar(value)
