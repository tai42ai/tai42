"""A date field on WhatsApp: whether it draws the ``CalendarPicker``, and its per-send bounds.

The contract lets a ``format: date`` and a ``format: date-time`` field carry ``minDate``/``maxDate``/
``unavailableDates`` (:func:`carries_date_bounds`). Their values change per send, so the published
Flow carries only WHICH bounds a field has, and each send carries their VALUES under screen-data
carriers (:data:`_CARRIERS`):

* ``<cname>__min`` (``minDate``) and ``<cname>__max`` (``maxDate``), a ``YYYY-MM-DD`` string;
* ``<cname>__unavailable``, the excluded explicit dates of ``unavailableDates``;
* ``<cname>__days``, Meta's weekday codes of the days ``unavailableDates`` leaves selectable.

A field that draws the ``CalendarPicker`` (:func:`draws_calendar_picker`) binds each carrier to its
control member; any other control (a ``date-time`` field's text or choice control, the choice
control a ``date`` field with a schema ``enum`` or per-send options draws) binds none. Carriers are declared,
sent and forwarded only where something reads them (:func:`reads_bound_carriers`): a
``CalendarPicker``, and in an ask-less endpoint-driven form the data endpoint, which reads every
date field's carriers on every screen (:func:`schema_with_sent_date_bounds`, checked against the
bounds each field declares, :func:`declared_date_bounds`). An ask's data endpoint checks the bounds
of a date field no ``CalendarPicker`` draws from the ask's own per-send schema, so such a field
carries none there. :func:`date_bound_keys` is the one answer to "which bounds a field has"; every builder of a
binding, a declaration, a forward and a send value goes through it. A merged ``rangeStart`` pair
draws one range control under its START, so its carriers sit under the START's component name and
hold the pair's merged bounds, never looser than either end.
"""

from __future__ import annotations

import copy
import datetime
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from tai42_contract.channels import ChannelInputError
from tai42_kit.interactions import FORM_WEEKDAYS

from tai42_channel_whatsapp.flows_choices import _is_choice_field

# Meta's ``CalendarPicker`` (Flow JSON ≥ 6.1) sets and returns a ``YYYY-MM-DD`` string. The regex
# fixes the hyphenated calendar-date form; ``date.fromisoformat`` then rejects an impossible date
# (e.g. ``2026-13-40``).
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Each platform weekday name an ``unavailableDates`` entry may carry (``tai42_kit.interactions``),
# mapped onto Meta's ``include-days`` enum code.
_WEEKDAY_CODES = {
    "monday": "Mon",
    "tuesday": "Tue",
    "wednesday": "Wed",
    "thursday": "Thu",
    "friday": "Fri",
    "saturday": "Sat",
    "sunday": "Sun",
}

# The schema bound keys, each with the type a declared value has.
_BOUND_KEY_TYPES: dict[str, type] = {"minDate": str, "maxDate": str, "unavailableDates": list}
DATE_BOUND_KEYS = tuple(_BOUND_KEY_TYPES)


@dataclass(frozen=True)
class _Carrier:
    """One per-send bound carrier: its suffix, schema key, bound control member, and whether it holds a list."""

    suffix: str
    key: str
    member: str
    holds_list: bool

    def name(self, cname: str) -> str:
        return f"{cname}{self.suffix}"

    def decl(self) -> dict[str, Any]:
        """The screen-``data`` declaration of the carrier: a string, or an array of strings."""
        if self.holds_list:
            return {"type": "array", "items": {"type": "string"}, "__example__": []}
        return {"type": "string", "__example__": ""}


_MIN = _Carrier("__min", "minDate", "min-date", holds_list=False)
_MAX = _Carrier("__max", "maxDate", "max-date", holds_list=False)
_UNAVAILABLE = _Carrier("__unavailable", "unavailableDates", "unavailable-dates", holds_list=True)
_DAYS = _Carrier("__days", "unavailableDates", "include-days", holds_list=True)
_CARRIERS = (_MIN, _MAX, _UNAVAILABLE, _DAYS)
DATE_BOUND_SUFFIXES = tuple(carrier.suffix for carrier in _CARRIERS)


def carries_date_bounds(prop: Any) -> bool:
    """Whether a property's date bounds ride the per-send carriers — a ``string`` of ``format`` ``date``/``date-time``.

    These are the two formats the contract gives ``minDate``/``maxDate``/``unavailableDates``.
    """
    return isinstance(prop, dict) and prop.get("type") == "string" and prop.get("format") in ("date", "date-time")


def is_date_field(prop: Any) -> bool:
    """Whether a property holds a calendar date — a ``string`` carrying ``"format": "date"``.

    ``format: time`` and ``format: date-time`` are not this: the vendor has no time-of-day
    picker, so they draw no ``CalendarPicker``.
    """
    return isinstance(prop, dict) and prop.get("type") == "string" and prop.get("format") == "date"


def draws_calendar_picker(name: str, prop: Any, option_fields: set[str]) -> bool:
    """Whether a property renders as a date ``CalendarPicker``: a date field (:func:`is_date_field`) no choice draws.

    A date field with a schema ``enum`` or per-send options (``option_fields``, the send's
    option-bearing fields) renders as its choice control instead, as the Flow's control
    builder decides it.
    """
    return is_date_field(prop) and not _is_choice_field(name, prop, option_fields)


def valid_iso_date(value: str) -> bool:
    """Whether ``value`` is a ``YYYY-MM-DD`` calendar date the ``CalendarPicker`` accepts."""
    if not _ISO_DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def date_bound_keys(prop: Any, range_end: Any = None) -> frozenset[str]:
    """The bound keys a date field declares: ``minDate``/``maxDate`` as a string, ``unavailableDates`` as a list.

    ``range_end`` is the END's definition when ``prop`` is a merged range START: the pair's one
    control has a bound when either end declares it. A property whose bounds ride no carrier
    (:func:`carries_date_bounds`) has none.
    """
    return frozenset(
        key
        for end in (prop, range_end)
        if carries_date_bounds(end)
        for key, value_type in _BOUND_KEY_TYPES.items()
        if isinstance(end.get(key), value_type)
    )


def _declared_carriers(prop: Any, range_end: Any) -> list[_Carrier]:
    # The carriers of the bounds a date field declares (:func:`date_bound_keys`), in table order.
    keys = date_bound_keys(prop, range_end)
    return [carrier for carrier in _CARRIERS if carrier.key in keys]


def reads_bound_carriers(name: str, prop: Any, option_fields: set[str], *, every_screen: bool) -> bool:
    """Whether anything reads a date field's bound carriers, so they are declared, sent and forwarded.

    A ``CalendarPicker`` (:func:`draws_calendar_picker`) binds them. With ``every_screen``
    (an ask-less endpoint-driven form, whose data endpoint checks each bound at the submit from the
    carriers the request echoes, so every screen carries them all) the data endpoint reads
    every date field's at each step, a ``date-time`` field's and a choice-drawn date field's
    included. Elsewhere such a field's carriers have no reader: an ask's data endpoint checks its
    bounds from the ask's own per-send schema.
    """
    return every_screen or draws_calendar_picker(name, prop, option_fields)


def _control_carriers(name: str, prop: Any, option_fields: set[str], range_end: Any) -> list[_Carrier]:
    # The declared carriers a field's own control reads: a CalendarPicker's, none for any other control.
    if not reads_bound_carriers(name, prop, option_fields, every_screen=False):
        return []
    return _declared_carriers(prop, range_end)


def date_bound_bindings(cname: str, prop: Any, range_end: Any = None) -> dict[str, str]:
    """The ``CalendarPicker`` members binding each declared bound to its carrier (``"${data.<carrier>}"``).

    Called only for a field drawn as a ``CalendarPicker`` (a merged range START with its END's
    definition as ``range_end``).
    """
    return {carrier.member: f"${{data.{carrier.name(cname)}}}" for carrier in _declared_carriers(prop, range_end)}


def control_bound_decls(
    name: str, cname: str, prop: Any, option_fields: set[str], range_end: Any = None
) -> dict[str, Any]:
    """The screen-``data`` declarations of the bound carriers a date field's ``CalendarPicker`` binds (else none)."""
    return {carrier.name(cname): carrier.decl() for carrier in _control_carriers(name, prop, option_fields, range_end)}


def control_bound_forwards(
    name: str, cname: str, prop: Any, option_fields: set[str], range_end: Any = None
) -> dict[str, str]:
    """The navigate-payload entries carrying the bound carriers a date field's control binds to the next screen."""
    return {
        carrier.name(cname): f"${{data.{carrier.name(cname)}}}"
        for carrier in _control_carriers(name, prop, option_fields, range_end)
    }


def date_bound_decls(cname: str, prop: Any, range_end: Any = None) -> dict[str, Any]:
    """The screen-``data`` declarations of every bound carrier a date field declares: a string or a string array."""
    return {carrier.name(cname): carrier.decl() for carrier in _declared_carriers(prop, range_end)}


def date_bound_values(cname: str, prop: Any, range_end: Any = None) -> dict[str, Any]:
    """The per-send values of a date field's bound carriers.

    For a merged range START (``range_end`` given) the values are never looser than either end:
    ``__min`` is the later ``minDate``, ``__max`` the earlier ``maxDate`` (ISO dates sort lexically),
    and the exclusions are the union of both ends'. ``__unavailable`` holds the excluded explicit
    dates (order kept, de-duplicated); ``__days`` the codes of every weekday not excluded, all seven
    when none is.
    """
    keys = date_bound_keys(prop, range_end)
    ends = [end for end in (prop, range_end) if carries_date_bounds(end)]
    values: dict[str, Any] = {}
    if _MIN.key in keys:
        values[_MIN.name(cname)] = max(end[_MIN.key] for end in ends if isinstance(end.get(_MIN.key), str))
    if _MAX.key in keys:
        values[_MAX.name(cname)] = min(end[_MAX.key] for end in ends if isinstance(end.get(_MAX.key), str))
    if _UNAVAILABLE.key in keys:
        dates, days = _union_exclusions(ends)
        values[_UNAVAILABLE.name(cname)] = dates
        values[_DAYS.name(cname)] = [_WEEKDAY_CODES[day] for day in FORM_WEEKDAYS if day not in days]
    return values


def merged_range_bound_values(cname: str, start: str, end: str, properties: dict[str, Any]) -> dict[str, Any]:
    """The per-send bound values of a merged ``rangeStart`` pair, carried under its START's ``cname``.

    The pair's one range control enforces the merged bounds (:func:`date_bound_values`) on both
    ends. Raises ``ChannelInputError`` naming the pair when the merged ``minDate`` is later than the
    merged ``maxDate`` (each end's own bounds are in order, but together they leave no date the
    control can offer). Whether a pair merges is decided by
    :func:`~tai42_channel_whatsapp.flows_components.range_pairs`, which reads no bound key, so the
    pair cannot fall back to two controls here.
    """
    values = date_bound_values(cname, properties[start], properties[end])
    earliest, latest = values.get(_MIN.name(cname)), values.get(_MAX.name(cname))
    if earliest is not None and latest is not None and earliest > latest:
        raise ChannelInputError(
            f"form fields {start!r} and {end!r} draw as one WhatsApp range control whose merged "
            f"minDate {earliest!r} is later than its merged maxDate {latest!r}, so no date can be picked"
        )
    return values


def _union_exclusions(ends: list[dict[str, Any]]) -> tuple[list[str], set[str]]:
    # The union of the ends' excluded explicit dates (order kept, de-duplicated) and excluded weekday
    # names; an entry that is neither is skipped.
    dates: list[str] = []
    days: set[str] = set()
    for end in ends:
        unavailable = end.get(_UNAVAILABLE.key)
        if not isinstance(unavailable, list):
            continue
        for entry in unavailable:
            if not isinstance(entry, str):
                continue
            if _ISO_DATE_RE.match(entry) and entry not in dates:
                dates.append(entry)
            elif entry.lower() in FORM_WEEKDAYS:
                days.add(entry.lower())
    return dates, days


def date_field_ends(
    fields: list[str], properties: dict[str, Any], range_merges: dict[str, str]
) -> Iterator[tuple[str, str | None]]:
    """Each field among ``fields`` whose bounds ride carriers under its name, with its merged END (else ``None``).

    A field whose bounds ride carriers is one :func:`carries_date_bounds` names. A merged END (a key
    of ``range_merges``, the ``END -> START`` map) draws no control: its bounds ride its START's
    carriers.
    """
    merged_starts = {start: end for end, start in range_merges.items()}
    for name in fields:
        if name not in range_merges and carries_date_bounds(properties.get(name)):
            yield name, merged_starts.get(name)


def forwarded_bound_carriers(
    properties: dict[str, Any],
    names: dict[str, str],
    option_fields: set[str],
    range_merges: dict[str, str],
    later_fields: list[str],
    *,
    every_screen: bool,
) -> list[str]:
    """The bound carriers a step answer forwards to the next screen.

    With ``every_screen`` every date field's declared carriers (the data endpoint reads them at each
    step); else those of each field among ``later_fields`` whose carriers are read
    (:func:`reads_bound_carriers`), which only the next screens' ``CalendarPicker`` controls bind.
    """
    fields = list(properties) if every_screen else later_fields
    return [
        carrier.name(names[name])
        for name, end in date_field_ends(fields, properties, range_merges)
        if reads_bound_carriers(name, properties[name], option_fields, every_screen=every_screen)
        for carrier in _declared_carriers(properties[name], properties.get(end) if end else None)
    ]


def schema_without_date_bound_values(schema: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``schema`` with each date field's ``minDate``/``maxDate``/``unavailableDates`` removed.

    What remains is what the published Flow and an ask-less form's sidecar read of the schema: a
    date field's bound values ride the per-send data (:func:`date_bound_values`),
    never the Flow, which declares only the bounds the field has.
    """
    stripped = copy.deepcopy(schema)
    properties = stripped.get("properties")
    for prop in properties.values() if isinstance(properties, dict) else ():
        if carries_date_bounds(prop):
            for key in DATE_BOUND_KEYS:
                prop.pop(key, None)
    return stripped


def declared_date_bounds(properties: dict[str, Any], range_merges: dict[str, str]) -> dict[str, list[str]]:
    """The declared bound keys (:func:`date_bound_keys`) of each field whose carriers sit under its name, sorted.

    Keyed as the Flow declares the carriers (:func:`date_field_ends`): a merged ``rangeStart`` pair
    under its START, holding the keys either end declares; ``range_merges`` is the form's ``END ->
    START`` map. Which carriers a form has is fixed per published Flow (the publish key keeps it while
    :func:`schema_without_date_bound_values` drops the values), so an ask-less form's sidecar stores
    this beside its stripped schema: it names the carriers every request must echo, one value per
    published Flow. A field declaring none is left out.
    """
    declared: dict[str, list[str]] = {}
    for name, end in date_field_ends(list(properties), properties, range_merges):
        keys = date_bound_keys(properties[name], properties.get(end) if end else None)
        if keys:
            declared[name] = sorted(keys)
    return declared


def schema_with_sent_date_bounds(
    schema: dict[str, Any],
    names: dict[str, str],
    range_merges: dict[str, str],
    declared: dict[str, list[str]],
    data: dict[str, Any],
) -> dict[str, Any]:
    """A copy of ``schema`` with each date field's declared bounds read back from the carriers ``data`` echoes.

    ``declared`` is :func:`declared_date_bounds` of the form the Flow was published from; each
    declared bound's carriers are read (``__min`` → ``minDate``, ``__max`` → ``maxDate``,
    ``__unavailable`` and ``__days`` → ``unavailableDates``: the excluded dates, then the weekday
    names whose code ``__days`` leaves out). A merged range START's carriers hold the bounds either
    end declares and bound both ends, as the pair's one control does. Raises ``ChannelInputError``
    naming the carrier when a declared one is missing from ``data`` or not the shape the send gave it.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    out = copy.deepcopy(schema)
    for name, end in date_field_ends(list(properties), properties, range_merges):
        bounds = _sent_bounds(names[name], set(declared.get(name, ())), data)
        for target in (name, end):
            if target is not None and bounds:
                out["properties"][target].update(bounds)
    return out


def _sent_bounds(cname: str, keys: set[str], data: dict[str, Any]) -> dict[str, Any]:
    # The schema bounds ``keys`` that the carriers of ``cname`` in ``data`` stand for.
    for carrier in _CARRIERS:
        if carrier.key in keys and carrier.name(cname) not in data:
            raise ChannelInputError(f"date bound carrier {carrier.name(cname)!r} is missing from the request")
    bounds: dict[str, Any] = {}
    for carrier in (_MIN, _MAX):
        if carrier.key not in keys:
            continue
        name = carrier.name(cname)
        value = data[name]
        if not (isinstance(value, str) and valid_iso_date(value)):
            raise ChannelInputError(f"date bound carrier {name!r} must be a 'YYYY-MM-DD' string, got {value!r}")
        bounds[carrier.key] = value
    if _UNAVAILABLE.key in keys:
        dates = _sent_strings(_UNAVAILABLE.name(cname), data)
        kept = _sent_strings(_DAYS.name(cname), data)
        excluded = [day for day in FORM_WEEKDAYS if _WEEKDAY_CODES[day] not in kept]
        bounds[_UNAVAILABLE.key] = [*dates, *excluded]
    return bounds


def _sent_strings(carrier: str, data: dict[str, Any]) -> list[str]:
    value = data[carrier]
    if not (isinstance(value, list) and all(isinstance(entry, str) for entry in value)):
        raise ChannelInputError(f"date bound carrier {carrier!r} must be a list of strings, got {value!r}")
    return value
