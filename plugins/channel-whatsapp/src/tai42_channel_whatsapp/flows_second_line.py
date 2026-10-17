"""A form field's and a form option's second line on a WhatsApp Flow.

A field's optional second line (its schema ``description``) draws as the control's ``helper-text``
where Meta allows it (text and date controls, at most 80 characters) and as a sibling
``TextCaption`` otherwise (a choice control / opt-in, at most 409 characters); a longer one is
refused naming the property. An option's optional second line (its ``description``) draws under the
option title in the data-source item of a dropdown / radio group / checkbox group (at most 300
characters, else refused), and is folded onto the chip title after an em dash on a chip grid, whose
item has no second-line slot. The data-source rows of every send and reaction door are built here.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.channels import ChannelInputError

from tai42_channel_whatsapp.flows_choices import _CHIPS_MAX_CHOICES, _CHIPS_MIN_CHOICES, _draws_chips

# Meta's cap on a dropdown / radio group / checkbox group data-source item ``description``.
_DS_ITEM_DESCRIPTION_MAX_CHARS = 300
# Meta's cap on a text / date control's ``helper-text``.
_HELPER_TEXT_MAX_CHARS = 80
# Meta's cap on a ``TextCaption``'s text.
_TEXT_CAPTION_MAX_CHARS = 409


#: The Flow controls that take a ``helper-text`` (a field's second line drawn under its label):
#: Meta allows it on the text and single-date inputs, NOT on a Dropdown/RadioButtonsGroup/
#: CheckboxGroup/ChipsSelector/OptIn. A field's second line on a control outside this set — and on a
#: range-mode CalendarPicker, whose paired ``{start-date, end-date}`` members are drawn by
#: :func:`range_captions` — is drawn as a sibling ``TextCaption`` instead (:func:`field_caption`), so
#: its content is never dropped. Meta caps ``helper-text`` at 80 characters and a ``TextCaption`` at
#: 409; a longer second line is refused at build naming the property, never truncated.
_HELPER_TEXT_TYPES = frozenset({"TextInput", "TextArea", "DatePicker", "CalendarPicker"})


def _field_second_line(prop: dict[str, Any]) -> str | None:
    """A field's optional second line — its schema ``description`` when a non-blank string, else None.

    A blank/whitespace-only description counts as absent (nothing to draw), the same rule
    :func:`_field_label` applies to a blank ``title``.
    """
    description = prop.get("description")
    return description if isinstance(description, str) and description.strip() else None


def _capped_second_line(name: str, line: str, cap: int, slot: str) -> str:
    """Return ``line`` when it fits Meta's ``cap`` for ``slot``; refuse it naming the property otherwise."""
    if len(line) > cap:
        raise ChannelInputError(
            f"form property {name!r} description is {len(line)} characters; WhatsApp caps {slot} at {cap}"
        )
    return line


def _with_helper_text(name: str, component: dict[str, Any], prop: dict[str, Any]) -> dict[str, Any]:
    """Add the field's second line as ``helper-text`` when the control supports it.

    A control outside :data:`_HELPER_TEXT_TYPES` (a choice control or an OptIn) keeps no
    helper-text; its second line is drawn as a sibling :func:`field_caption` so it is never lost.
    A line past Meta's 80-character helper-text cap raises ``ChannelInputError`` naming the property.
    """
    description = _field_second_line(prop)
    if description is not None and component["type"] in _HELPER_TEXT_TYPES:
        component["helper-text"] = _capped_second_line(
            name, description, _HELPER_TEXT_MAX_CHARS, "a field's helper text"
        )
    return component


def field_caption(name: str, component: dict[str, Any], prop: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``TextCaption`` node carrying a field's second line for a control that cannot hold ``helper-text``.

    Empty when the field has no second line or its control already drew it as ``helper-text``
    (:data:`_HELPER_TEXT_TYPES`). The caption is emitted immediately after the control and, for a
    conditional field, inside the same ``If`` wrapper so it shows and hides with the control. A line
    past Meta's 409-character caption cap raises ``ChannelInputError`` naming the property.
    """
    description = _field_second_line(prop)
    if description is None or component["type"] in _HELPER_TEXT_TYPES:
        return []
    return [
        {"type": "TextCaption", "text": _capped_second_line(name, description, _TEXT_CAPTION_MAX_CHARS, "a caption")}
    ]


def range_captions(
    start_name: str, start_prop: dict[str, Any], end_name: str, end_prop: dict[str, Any]
) -> list[dict[str, Any]]:
    """The ``TextCaption`` nodes carrying a merged ``rangeStart`` pair's second lines, START first.

    A range-mode CalendarPicker draws both ends as ONE control, so each end's non-blank schema
    ``description`` is drawn as its own caption after the control (in the pair's ``If`` wrapper for
    a conditional pair); an end without one draws none. An end's line past Meta's 409-character
    caption cap raises ``ChannelInputError`` naming that end's property.
    """
    captions: list[dict[str, Any]] = []
    for name, prop in ((start_name, start_prop), (end_name, end_prop)):
        line = _field_second_line(prop)
        if line is not None:
            captions.append(
                {"type": "TextCaption", "text": _capped_second_line(name, line, _TEXT_CAPTION_MAX_CHARS, "a caption")}
            )
    return captions


# The data-source item type per control, fixed by the schema property alone so the published Flow
# (and its cache key) never depends on a given send's option list. A dropdown / radio group /
# checkbox group item always declares ``description`` (an option's second line); a chips item
# declares exactly the fields Meta's ChipsSelector reads.
_DS_ITEM_PROPERTIES = {"id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}}
_DS_ITEM_EXAMPLE = [{"id": "a", "title": "a", "description": "a"}]
_CHIPS_DS_ITEM_PROPERTIES = {"id": {"type": "string"}, "title": {"type": "string"}}
_CHIPS_DS_ITEM_EXAMPLE = [{"id": "a", "title": "a"}]


def data_source_item(name: str, prop: dict[str, Any], choice: dict[str, Any]) -> dict[str, str]:
    """One ``data-source`` row for a per-send option of property ``name`` (every send and reaction door).

    The option's second line (``description``) counts only when a non-blank string. A chip has no
    second-line slot, so on a field drawn as a ChipsSelector (:func:`_draws_chips`) the line is
    folded onto the chip title after an em dash (``Gold — 2-3 days``) and the row carries only
    ``id``/``title``; Meta documents no cap on the chip title, so none is applied. A dropdown / radio
    group / checkbox group draws the line under the title as the item ``description``, capped at
    Meta's 300 characters — a longer one raises ``ChannelInputError`` naming the property and option.
    """
    value = choice["value"]
    title = choice.get("label") or value
    raw = choice.get("description")
    description = raw if isinstance(raw, str) and raw.strip() else None
    if _draws_chips(prop):
        return {"id": value, "title": title if description is None else f"{title} \u2014 {description}"}
    item: dict[str, str] = {"id": value, "title": title}
    if description is not None:
        if len(description) > _DS_ITEM_DESCRIPTION_MAX_CHARS:
            raise ChannelInputError(
                f"form property {name!r} option {value!r} description is {len(description)} characters; "
                f"WhatsApp caps an option description at {_DS_ITEM_DESCRIPTION_MAX_CHARS}"
            )
        item["description"] = description
    return item


def data_source_rows(name: str, prop: dict[str, Any], option_list: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The ``data-source`` rows for a per-send option list of property ``name`` (every send and reaction door).

    A field drawn as a ChipsSelector (:func:`_draws_chips`, decided by its schema enum) can show only
    Meta's 2..20 choices, so a per-send list of another length is refused with ``ChannelInputError``
    naming the property, the count and the bound — never a chip grid fed rows past Meta's limit.
    Each row is built by :func:`data_source_item`.
    """
    if _draws_chips(prop) and not _CHIPS_MIN_CHOICES <= len(option_list) <= _CHIPS_MAX_CHOICES:
        raise ChannelInputError(
            f"form property {name!r} draws a chip grid but carries {len(option_list)} per-send options; "
            f"WhatsApp draws {_CHIPS_MIN_CHOICES}..{_CHIPS_MAX_CHOICES} chips"
        )
    return [data_source_item(name, prop, choice) for choice in option_list]
