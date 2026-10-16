"""Choice classification for the WhatsApp Flow builder.

Which control a choice property renders as: a multiple-choice array of strings, a string with a
schema ``enum`` or per-send options, the radio/dropdown split, and whether a ``control: chips``
choice draws a ``ChipsSelector`` within Meta's 2..20 bound (single- or multi-pick). The component
builders (:mod:`tai42_channel_whatsapp.flows_components`) and the second-line rows
(:mod:`tai42_channel_whatsapp.flows_second_line`) both read these.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.interactions import FormControl
from tai42_kit.interactions import RADIO_OPTION_THRESHOLD

# Meta's ``ChipsSelector`` option bounds: it renders at least 2 and at most 20 choices. A
# ``control: chips`` choice whose declared list falls outside this range cannot render as a
# ChipsSelector, so it DEGRADES to the shape's default control (a radio group / dropdown for a
# string, a checkbox group for an array) — a named, documented degrade, never a silent drop.
_CHIPS_MIN_CHOICES = 2
_CHIPS_MAX_CHOICES = 20


def _is_array_of_strings(prop: dict[str, Any]) -> bool:
    """Whether a property is an array whose ``items`` are strings — a multiple-choice field."""
    items = prop.get("items")
    return prop.get("type") == "array" and isinstance(items, dict) and items.get("type") == "string"


def _choice_options_enum(prop: dict[str, Any]) -> list[Any] | None:
    """A choice property's schema option list — a string's ``enum`` or an array's ``items.enum`` — else ``None``."""
    enum = prop["items"].get("enum") if _is_array_of_strings(prop) else prop.get("enum")
    return enum if isinstance(enum, list) else None


def _is_choice_field(name: str, prop: dict[str, Any], option_fields: set[str]) -> bool:
    """Whether a property renders as a dynamic CHOICE control (reads a per-send ``data-source``).

    A string that carries a schema ``enum`` or is marked option-bearing by the ask, OR an
    array of strings (multiple choice). Dropdown, RadioButtonsGroup and CheckboxGroup all read
    their options from the per-send ``${data.<field>__ds}`` list.
    """
    if _is_array_of_strings(prop):
        return True
    if prop.get("type") != "string":
        return False
    return prop.get("enum") is not None or name in option_fields


def _choice_component_type(prop: dict[str, Any]) -> str:
    """The Flow control a choice property renders as.

    An array of strings is a ``CheckboxGroup`` (multiple selection). A string choice with a
    schema ``enum`` of at most :data:`RADIO_OPTION_THRESHOLD` options is a ``RadioButtonsGroup``
    (the short-list control); any other string choice — a longer enum, or an option-bearing
    field whose per-send list length is not fixed at publish — stays the dynamic ``Dropdown``.
    """
    if _is_array_of_strings(prop):
        return "CheckboxGroup"
    enum = prop.get("enum")
    if isinstance(enum, list) and len(enum) <= RADIO_OPTION_THRESHOLD:
        return "RadioButtonsGroup"
    return "Dropdown"


def _draws_chips(prop: dict[str, Any]) -> bool:
    """Whether a ``control: chips`` choice property renders as a ``ChipsSelector`` on this channel.

    A ChipsSelector draws Meta's 2..20 choices. A ``chips`` choice whose declared list
    (:func:`_choice_options_enum` — a string's ``enum`` or an array's ``items.enum``, which the
    schema always carries for ``chips``) falls outside that range cannot render as one and DEGRADES
    to the shape's default control; a property not marked ``chips`` never draws one.
    """
    if prop.get("control") != FormControl.CHIPS:
        return False
    enum = _choice_options_enum(prop)
    return enum is not None and _CHIPS_MIN_CHOICES <= len(enum) <= _CHIPS_MAX_CHOICES


def _chips_single_pick(prop: dict[str, Any]) -> bool:
    """Whether a property renders as a SINGLE-pick ``ChipsSelector`` — a ``string`` chips choice.

    Its control value is a one-element LIST (``max-selected-items: 1``), so its screen-``data``
    ``init``/``__val`` carriers are array-typed and the inbound decode unwraps the one-element list
    back to the string. An ARRAY chips choice is already a list answer and needs no unwrap; a chips
    choice that degrades (outside Meta's 2..20) draws a scalar default control and is not this.
    """
    return prop.get("type") == "string" and _draws_chips(prop)
