"""The endpoint-driven half of a WhatsApp Flow form: when a form goes through the data endpoint, and what rides there.

A form is ENDPOINT-DRIVEN when it declares a consumer reaction, carries a shape no drawn Meta
control checks natively (:func:`~tai42_channel_whatsapp.flows_shapes.field_needs_endpoint`), or
draws a field as several per-case controls (a multi-value ``visibleWhen``, whose answer the data
endpoint reads from the shown case —
:func:`~tai42_channel_whatsapp.flows_visibility.answer_refs`); its every footer then fires a
``data_exchange`` the data endpoint answers — the step footers a ``page_advanced`` event, the
terminal footer a ``submitted`` event — and each reacting field a ``field_changed`` event. Each
payload carries the values filled so far keyed by schema name (a per-case field one entry per
case, keyed by the case's component name), the event markers, and the screen's whole ``data``
(:func:`screen_data_refs`): the data an endpoint answers with becomes the screen's state, so the
endpoint builds its answer — the same screen or the next one — from the request alone.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from tai42_contract.channels import ChannelInputError
from tai42_contract.interactions import SHAPE_KEYWORDS, FormReactionEventKind

from tai42_channel_whatsapp.flows_components import _field_control
from tai42_channel_whatsapp.flows_shapes import ERROR_MESSAGE_TYPES, field_needs_endpoint
from tai42_channel_whatsapp.flows_visibility import answer_refs, form_splits_fields

# The event markers an endpoint-driven Flow carries in a ``data_exchange`` action's payload so the
# data endpoint maps the vendor request onto an event. They are reserved property names on an
# endpoint-driven form (refused like ``flow_token``) so a field value can never collide with a
# marker in the same payload; a static form never carries them.
REACTION_EVENT_KEY = "tai42_event"
REACTION_FIELD_KEY = "tai42_field"
REACTION_PAGE_KEY = "tai42_page"
RESERVED_REACTION_KEYS = frozenset({REACTION_EVENT_KEY, REACTION_FIELD_KEY, REACTION_PAGE_KEY})

# The controls a ``field_changed`` reaction can ride: a choice, a calendar, an opt-in.
_SELECTABLE_CONTROLS = frozenset(
    {"Dropdown", "RadioButtonsGroup", "CheckboxGroup", "ChipsSelector", "CalendarPicker", "OptIn"}
)

# The per-field screen-data carriers, by suffix on the field's component name.
CARRIER_SUFFIXES = ("__init", "__ds", "__err", "__invalid", "__val")


def error_fields(
    properties: dict[str, Any],
    required: set[str],
    option_fields: set[str],
    names: dict[str, str],
    merged_ends: set[str],
) -> frozenset[str]:
    """The fields whose control binds an ``error-message`` — a merged range END binds through its START's control."""
    out = set(merged_ends)
    for name, prop in properties.items():
        if name in merged_ends:
            continue
        if _field_control(name, prop, name in required, option_fields, names)["type"] in ERROR_MESSAGE_TYPES:
            out.add(name)
    return frozenset(out)


def error_carriers(
    fields: Iterable[str],
    names: dict[str, str],
    bound: frozenset[str],
    range_merges: Mapping[str, str],
    sentences: Mapping[str, str],
) -> dict[str, Any]:
    """The ``__err`` screen-data value of each of ``fields`` whose control binds an ``error-message``.

    ``bound`` is :func:`error_fields` and ``range_merges`` the ``END -> START`` map of the pairs
    drawn as one range control. Each field's sentence comes from ``sentences`` (the empty string,
    no error shown, when absent). A merged range START's value is the object ``{start-date,
    end-date}`` its range control's ``error-message`` reads — the START's sentence on
    ``start-date``, its END's on ``end-date`` — so each end shows its own sentence; a merged END
    has no value of its own; every other field's is the sentence string.
    """
    starts = {start: end for end, start in range_merges.items()}
    out: dict[str, Any] = {}
    for name in fields:
        if name not in bound or name in range_merges:
            continue
        key = f"{names[name]}__err"
        if name in starts:
            out[key] = {"start-date": sentences.get(name, ""), "end-date": sentences.get(starts[name], "")}
        else:
            out[key] = sentences.get(name, "")
    return out


def shaped_fields(properties: dict[str, Any]) -> frozenset[str]:
    """The fields carrying a shape a value can break — the ones a caller's ``invalid`` sentence may word."""
    out: set[str] = set()
    for name, prop in properties.items():
        items = prop.get("items")
        item_keys = set(items) if isinstance(items, dict) else set()
        if any(key in prop or key in item_keys for key in SHAPE_KEYWORDS):
            out.add(name)
    return frozenset(out)


def form_endpoint_driven(
    properties: dict[str, Any],
    required: set[str],
    option_fields: set[str],
    names: dict[str, str],
    range_merges: dict[str, str],
    reacting: bool,
    fields_by_screen: list[list[str]],
) -> bool:
    """Whether the form goes through the data endpoint: it reacts, a shape is not native, or it splits a field.

    A field is split when it is drawn as several per-case controls. ``range_merges`` is the ``END ->
    START`` map of the pairs drawn as one range control (both ends of such a pair are native for
    their span and order); ``fields_by_screen`` the form's fields per screen, which decide whether a
    multi-value ``visibleWhen`` draws its field per case.
    """
    if reacting or form_splits_fields(properties, names, fields_by_screen):
        return True
    merged = set(range_merges) | set(range_merges.values())
    for name, prop in properties.items():
        component = _field_control(name, prop, name in required, option_fields, names)
        if field_needs_endpoint(prop, component, name in merged):
            return True
    return False


def guard_endpoint_names(
    properties: dict[str, Any], names: dict[str, str], slot_keys: Iterable[str], case_names: Iterable[str]
) -> None:
    """Refuse an endpoint-driven form whose component names or payload keys collide with each other.

    An endpoint-driven payload keys each value by its schema name — a per-case field's by its cases'
    component names (``case_names``, one entry per drawn control) — beside the event markers, every
    field's carriers (``<name>__init`` …) and the display slots' data keys (``slot_keys``); a key
    spelled like another would be read as the other. A per-case control also shares the screen's
    component namespace with every field's own control (``names``), so a case named like another
    field's control, or drawn by two fields, would bind two controls to one name. Raises
    ``ChannelInputError`` naming the colliding keys.
    """
    reserved = (
        set(RESERVED_REACTION_KEYS)
        | {f"{cname}{suffix}" for cname in names.values() for suffix in CARRIER_SUFFIXES}
        | set(slot_keys)
    )
    collision = sorted(reserved & set(properties))
    if collision:
        raise ChannelInputError(
            f"form property names {collision} collide with the reserved data-endpoint markers, field "
            f"carriers or display slots of this form ({sorted(RESERVED_REACTION_KEYS)}, "
            f"<field>{'/<field>'.join(CARRIER_SUFFIXES)} and slot_<slot>)"
        )
    drawn = Counter(case_names)
    repeated = sorted(case_name for case_name, count in drawn.items() if count > 1)
    if repeated:
        raise ChannelInputError(
            f"form keys {repeated} name more than one per-case control of the multi-value visibleWhen fields "
            "of this form"
        )
    case_collision = sorted((reserved | set(properties) | set(names.values())) & set(drawn))
    if case_collision:
        raise ChannelInputError(
            f"form keys {case_collision} name both a per-case control of a multi-value visibleWhen field and "
            "a property, field control, data-endpoint marker, field carrier or display slot of this form"
        )


def screen_data_refs(data_model: dict[str, Any]) -> dict[str, str]:
    """The ``${data.<key>}`` reference of every key a screen declares — the screen's whole data in a payload."""
    return {key: f"${{data.{key}}}" for key in data_model}


def event_payload(
    kind: FormReactionEventKind,
    marker_key: str | None,
    marker_value: str | None,
    this_fields: list[str],
    earlier_fields: list[str],
    names: dict[str, str],
    variants: dict[str, list[tuple[str, list[str]]]],
    range_merges: dict[str, str],
) -> dict[str, Any]:
    """A ``data_exchange`` action payload: the event markers plus the values filled so far.

    The event kind rides ``tai42_event``; a field/page trigger its ``tai42_field``/``tai42_page``
    marker. Each known value is keyed by its SCHEMA NAME (this screen's through its control's
    plain ``${form.…}`` reference, earlier ones through their ``__val`` carrier), so the data
    endpoint hands the platform values keyed exactly as the stored schema; a field drawn as several
    per-case controls rides one plain reference per case, keyed by the case's component name
    (:func:`~tai42_channel_whatsapp.flows_visibility.answer_refs`), and the endpoint keeps the shown
    case's value under the schema name. A merged range END emits no value of its own (its START
    carries the pair's object, which the endpoint splits); later (unfilled) fields are omitted.
    """
    merged_ends = set(range_merges)
    payload: dict[str, Any] = {REACTION_EVENT_KEY: kind.value}
    if marker_key is not None and marker_value is not None:
        payload[marker_key] = marker_value
    for name in this_fields:
        if name in merged_ends:
            continue
        payload.update(answer_refs(name, variants[name], names))
    for name in earlier_fields:
        if name in merged_ends:
            continue
        payload[name] = f"${{data.{names[name]}__val}}"
    return payload


def _attach_field_reaction(
    component: dict[str, Any],
    name: str,
    this_fields: list[str],
    earlier_fields: list[str],
    names: dict[str, str],
    variants: dict[str, list[tuple[str, list[str]]]],
    range_merges: dict[str, str],
    screen_data: dict[str, str],
) -> dict[str, Any]:
    """Attach a ``field_changed`` ``data_exchange`` action to a selectable control, or raise.

    The payload carries the event, the values so far and the screen's whole data (``screen_data``,
    :func:`screen_data_refs`). A choice/date/chips control carries the action on
    ``on-select-action``, an OptIn on ``on-click-action``; a plain text or numeric input has no
    on-change event on WhatsApp and is refused loudly (never silently dropped).
    """
    payload = event_payload(
        FormReactionEventKind.FIELD_CHANGED,
        REACTION_FIELD_KEY,
        name,
        this_fields,
        earlier_fields,
        names,
        variants,
        range_merges,
    )
    action = {"name": "data_exchange", "payload": {**payload, **screen_data}}
    ctype = component["type"]
    if ctype in _SELECTABLE_CONTROLS and ctype != "OptIn":
        component["on-select-action"] = action
    elif ctype == "OptIn":
        component["on-click-action"] = action
    else:
        raise ChannelInputError(
            f"form property {name!r}: a field_changed reaction needs a selectable control (a choice, "
            f"date, or opt-in); a {ctype} has no on-change event on WhatsApp"
        )
    return component


def _reaction_sets(
    reactions: dict[str, Any] | None, properties: dict[str, Any], option_fields: set[str], names: dict[str, str]
) -> tuple[frozenset[str], frozenset[str], bool, bool]:
    """The ``(field_changed, page_advanced, submitted, reacting)`` render triggers from a reactions block.

    ``reacting`` is True when any trigger is declared. A ``field_changed`` field must render as a
    selectable control (refused loudly otherwise).
    """
    if not reactions:
        return frozenset(), frozenset(), False, False
    field_changed = frozenset(str(field) for field in reactions.get("field_changed", []))
    page_advanced = frozenset(str(page) for page in reactions.get("page_advanced", []))
    submitted = bool(reactions.get("submitted", False))
    reacting = bool(field_changed or page_advanced or submitted)
    for name in field_changed:
        prop = properties.get(name)
        if not isinstance(prop, dict):
            raise ChannelInputError(f"form reaction field_changed names unknown property {name!r}")
        ctype = _field_control(name, prop, False, option_fields, names)["type"]
        if ctype not in _SELECTABLE_CONTROLS:
            raise ChannelInputError(
                f"form property {name!r}: a field_changed reaction needs a selectable control (a choice, "
                f"date, or opt-in); a {ctype} has no on-change event on WhatsApp"
            )
    return field_changed, page_advanced, submitted, reacting
