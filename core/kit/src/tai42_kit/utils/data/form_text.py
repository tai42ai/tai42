"""Render a completed form's answer values as readable ``label: value`` text."""

import json
from typing import Any


def render_form_text(answer: dict[str, Any], schema: dict[str, Any] | None = None) -> str:
    """A completed form's readable, ALWAYS non-empty ``label: value`` lines.

    ``answer`` is the completed form's values; ``schema`` is the optional labelling
    context (it may be absent). A field's label is its schema property ``title``
    when that is a non-blank string, else the raw field key. Schema-named fields
    render first in schema order, then any answer keys the schema does not name
    (nothing is dropped); with no schema, answer insertion order is kept.

    A string value renders verbatim; every non-string value (bool, number, list,
    object) renders as compact JSON — so a bool is ``true``/``false`` (never a
    Python ``repr`` such as ``True``/``False``), Unicode is preserved, and a
    non-finite float raises loudly rather than passing silently. An answer that
    renders to no lines falls back to a compact JSON dump of the whole answer, so a
    consumer that rejects blank text is never handed an empty string.
    """
    properties = schema.get("properties") if isinstance(schema, dict) else None
    labels: dict[str, str] = {}
    if isinstance(properties, dict):
        for key, prop in properties.items():
            title = prop.get("title") if isinstance(prop, dict) else None
            labels[key] = title if isinstance(title, str) and title.strip() else key

    ordered = [key for key in labels if key in answer]
    ordered += [key for key in answer if key not in labels]

    lines: list[str] = []
    for key in ordered:
        value = answer[key]
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
        lines.append(f"{labels.get(key, key)}: {rendered}")

    text = "\n".join(lines)
    if not text:
        return json.dumps(answer, ensure_ascii=False, allow_nan=False)
    return text
