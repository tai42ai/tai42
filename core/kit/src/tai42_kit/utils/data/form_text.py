"""Render a completed form's answer values as readable ``label: value`` text."""

import json
from typing import Any

from tai42_kit.utils.data.number_text import number_text


def render_form_text(answer: dict[str, Any], schema: dict[str, Any] | None = None) -> str:
    """A completed form's readable, ALWAYS non-empty ``label: value`` lines.

    ``answer`` is the completed form's values; ``schema`` is the optional labelling
    context (it may be absent). A field's label is its schema property ``title``
    when that is a non-blank string, else the raw field key. Schema-named fields
    render first in schema order, then any answer keys the schema does not name
    (nothing is dropped); with no schema, answer insertion order is kept.

    A string value renders verbatim; every other value (bool, number, list, object)
    renders as JSON (:func:`_json_text`) — so a bool is ``true``/``false`` (never a
    Python ``repr`` such as ``True``/``False``), an integer keeps every digit, a
    float reads as a display slot writes it wherever it sits, a list or an object
    included (:func:`~tai42_kit.utils.data.number_text.number_text`: ``10.0`` reads
    ``10``), a non-finite float raises loudly rather than passing silently, and
    Unicode is preserved. An answer that renders to no lines falls back to the
    JSON of the whole answer, so a consumer that rejects blank text is never handed
    an empty string.
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
        rendered = value if isinstance(value, str) else _json_text(value)
        lines.append(f"{labels.get(key, key)}: {rendered}")

    text = "\n".join(lines)
    if not text:
        return _json_text(answer)
    return text


def _json_text(value: Any) -> str:
    """``value`` as JSON (``json.dumps``'s separators, Unicode kept), every float written by ``number_text``.

    A float inside a list or an object reads as it does on its own (``[10.0]`` reads ``[10]``); an
    object key that is not a string raises ``TypeError``, since an answer's keys are JSON's.
    """
    if isinstance(value, float):
        return number_text(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_json_text(item) for item in value) + "]"
    if isinstance(value, dict):
        members: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"an answer object key must be a string, got {key!r}")
            members.append(f"{json.dumps(key, ensure_ascii=False)}: {_json_text(item)}")
        return "{" + ", ".join(members) + "}"
    return json.dumps(value, ensure_ascii=False)
