"""The answer a submitted form modal carries, read from its ``view_submission`` state.

Each field's submitted element value is read back by the element type the modal drew for it
(:mod:`tai42_channel_slack.form_blocks`) and lifted to the JSON type its schema property
declares; the answer door validates the result against the schema.
"""

from __future__ import annotations

import math
from typing import Any

from tai42_channel_slack.form_blocks import FIELD_ACTION_ID
from tai42_channel_slack.forms import form_properties

__all__ = ["extract_answer"]


def _raw_value(entry: Any) -> str | None:
    """The submitted string of one scalar field, or ``None`` when it was left empty."""
    if not isinstance(entry, dict):
        return None
    kind = entry.get("type")
    if kind in ("plain_text_input", "number_input", "email_text_input", "url_text_input"):
        value = entry.get("value")
        return value if isinstance(value, str) and value != "" else None
    if kind in ("datepicker", "timepicker"):
        # Slack keys the chosen value by element (``string | null``); an empty picker is
        # an unfilled field, like an empty text input. The ISO string returns unchanged.
        value = entry.get("selected_date" if kind == "datepicker" else "selected_time")
        return value if isinstance(value, str) and value != "" else None
    if kind in ("static_select", "radio_buttons"):
        selected = entry.get("selected_option")
        if isinstance(selected, dict):
            value = selected.get("value")
            return value if isinstance(value, str) else None
        return None
    return None


def _array_value(entry: Any) -> list[str]:
    """The submitted list of one array field — an empty list when it was left unfilled.

    A ``multi_static_select``/``checkboxes`` field reads its ``selected_options`` values; a
    free multiple-choice ``plain_text_input`` splits its text one entry per non-blank line.
    """
    if not isinstance(entry, dict):
        return []
    kind = entry.get("type")
    if kind in ("multi_static_select", "checkboxes"):
        selected = entry.get("selected_options")
        if not isinstance(selected, list):
            return []
        return [opt["value"] for opt in selected if isinstance(opt, dict) and isinstance(opt.get("value"), str)]
    if kind == "plain_text_input":
        value = entry.get("value")
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
    return []


def _coerce(spec: dict[str, Any], raw: str) -> Any:
    """Lift a submitted string to the JSON type its schema property declares.

    Slack delivers every scalar input as a string. ``"true"``/``"false"`` for a
    ``boolean``, an integer literal for an ``integer`` and a finite literal for a
    ``number`` become the typed value; any other string is returned as submitted,
    and the answer door rejects a value of the wrong type against the schema with a
    field-located reason the modal shows inline. A non-finite ``number`` literal
    stays a string on purpose: as a float it would pass ``type: number`` and
    serialize to null.
    """
    ptype = spec.get("type")
    if ptype == "boolean":
        if raw == "true":
            return True
        if raw == "false":
            return False
        return raw
    if ptype == "integer":
        try:
            return int(raw)
        except ValueError:
            return raw
    if ptype == "number":
        try:
            parsed = float(raw)
        except ValueError:
            return raw
        # A non-finite float passes ``type: number`` and serializes to null; the
        # string does not pass, so it is forwarded for the door to reject.
        if not math.isfinite(parsed):
            return raw
        return parsed
    return raw


def extract_answer(schema: dict[str, Any], state_values: dict[str, Any]) -> dict[str, Any]:
    """The answer dict from a ``view_submission`` state.

    Each present, non-empty field lifted to its schema type when its string denotes one, else
    forwarded as submitted (an array field returns a list, an unfilled optional field is
    omitted). A field whose block is absent from the state — a
    field hidden by its conditional predicate before submit — is omitted; the answer facet
    drops any hidden field left in the answer besides.
    """
    answer: dict[str, Any] = {}
    for name, spec in form_properties(schema).items():
        field_name = str(name)
        block = state_values.get(field_name)
        if not isinstance(block, dict):
            continue
        entry = block.get(FIELD_ACTION_ID)
        spec_dict = spec if isinstance(spec, dict) else {}
        if spec_dict.get("type") == "array":
            values = _array_value(entry)
            if values:
                answer[field_name] = values
            continue
        raw = _raw_value(entry)
        if raw is None:
            continue
        answer[field_name] = _coerce(spec_dict, raw)
    return answer
