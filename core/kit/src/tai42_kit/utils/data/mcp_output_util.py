"""Pure extraction of an MCP tool response into a plain Python value.

Generic helpers usable by any plugin: detect a tool error, pull the error text,
and reduce a tool response (structured content, or a text/JSON content list) to a
deterministic value for programmatic (no-agent) use. No monitoring or framework
coupling — callers that want to annotate a span on error do so at the call site.
"""

from __future__ import annotations

import json
import re
from typing import Any


def _safe_get(obj: Any, camel_key: str, snake_key: str | None = None, default: Any = None) -> Any:
    if snake_key is None:
        snake_key = re.sub(r"(?<!^)(?=[A-Z])", "_", camel_key).lower()
    if isinstance(obj, dict):
        return obj.get(camel_key, obj.get(snake_key, default))
    return getattr(obj, camel_key, getattr(obj, snake_key, default))


def tool_has_error(response: Any) -> bool:
    """Whether an MCP tool response is flagged as an error (``isError``/``is_error``)."""
    return bool(_safe_get(response, "isError", "is_error", default=False))


def extract_tool_error(response: Any) -> str:
    """Pull the human-readable error text from an MCP tool response.

    Reads the first content item (dict ``text`` key, an object ``.text``
    attribute, or a plain string), falling back to ``"Unknown error"`` when no
    text is present.
    """
    content = _safe_get(response, "content", default=[])
    error_text = "Unknown error"
    if content and isinstance(content, list) and len(content) > 0:
        first_item = content[0]
        if isinstance(first_item, dict):
            error_text = first_item.get("text", error_text)
        elif hasattr(first_item, "text"):
            error_text = first_item.text or error_text
        elif isinstance(first_item, str):
            error_text = first_item
    return str(error_text)


def extract_tool_output(response: Any) -> Any:
    """
    Robustly extracts the output from an MCP tool response.
    Optimized for deterministic, programmatic use (no agent).
    Returns the full structured object to preserve metadata and context.
    An error response is returned unchanged.
    """

    is_error = _safe_get(response, "isError", "is_error", default=False)
    if is_error:
        return response

    # 1. Prefer structured content
    structured = _safe_get(response, "structuredContent", "structured_content")
    if structured is not None:
        return structured

    # 2. Fallback to Content Parsing
    content = _safe_get(response, "content", default=None)
    if not content or isinstance(content, str):
        return content

    # Handle List/Tuple (Standard MCP content list)
    if isinstance(content, (list, tuple)):
        parsed_items = []
        for item in content:
            # Extract raw text from TextContent objects or strings
            text_val = item
            if hasattr(item, "type") and item.type == "text" and hasattr(item, "text"):
                text_val = item.text

            text_val_str = str(text_val).strip()

            # Attempt JSON parsing on the item
            try:
                parsed_items.append(json.loads(text_val_str))
            except (json.JSONDecodeError, TypeError):
                parsed_items.append(text_val_str)

        # Return single item if only one exists, otherwise the full list
        if len(parsed_items) == 1:
            return parsed_items[0]
        return parsed_items

    return content
