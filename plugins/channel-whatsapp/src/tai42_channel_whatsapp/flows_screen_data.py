"""The render spec every Flow screen reads, and the screen-``data`` block it yields.

:class:`RenderSpec` carries the cross-cutting inputs of one Flow build. From it each screen
declares its ``data`` model (:func:`screen_data_model`): every field's init/ds/err, the ``__val``
carriers of fields collected earlier, the display slots of its own and every later page, and the
carriers an ask-less endpoint-driven form rides on every screen; and forwards the carriers its later
fields and later pages' slots still need (:func:`later_carriers`). Everything a later screen needs
enters at the entry screen, so each step forwards what its successors still read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tai42_channel_whatsapp.flows_components import (
    field_data_decls,
    forward_field_data,
    range_value_type_decl,
    value_type_decl,
)
from tai42_channel_whatsapp.flows_dates import date_bound_decls, date_field_ends
from tai42_channel_whatsapp.flows_endpoint import FORM_LINE_MEMBERS, form_line_carrier, required_fields, shaped_fields
from tai42_channel_whatsapp.flows_resolve import slot_keys
from tai42_channel_whatsapp.flows_shapes import ERROR_EXAMPLE


@dataclass(frozen=True)
class RenderSpec:
    """The cross-cutting inputs every screen reads while a Flow is built.

    ``field_changed`` are the schema keys whose change fires a reaction; ``page_advanced`` the
    page TITLES whose advance fires one; ``submitted`` whether the submission is reaction-checked.
    ``endpoint`` is True for an endpoint-driven Flow (a reacting form, or one carrying a shape no
    control checks natively) and drives the ``data_exchange`` footers + ``data_api_version``.
    ``slots`` maps each display slot to its screen-``data`` key. ``field_invalid`` is the caller's
    per-field ``invalid`` sentence; ``err_fields`` the fields binding an ``error-message``.
    ``askless_endpoint`` is True for an ask-less endpoint-driven form: it has no per-send sidecar, so
    its data endpoint reads the caller's words and every date field's bounds from the request, and
    every screen declares their carriers (:func:`_every_screen_decls`).
    ``boolean_words`` maps a boolean field to the
    ``(true, false)`` words its review readback draws, for the fields whose caller set at least one.
    """

    properties: dict[str, Any]
    required: set[str]
    option_fields: set[str]
    names: dict[str, str]
    labels: dict[str, str]
    slots: dict[str, str]
    field_changed: frozenset[str]
    page_advanced: frozenset[str]
    submitted: bool
    endpoint: bool
    # ``END name -> START name`` for every ``rangeStart`` pair drawn as ONE range control; a merged
    # END emits no control/data and a merged START carries the pair's object value.
    range_merges: dict[str, str]
    field_invalid: dict[str, str]
    err_fields: frozenset[str]
    askless_endpoint: bool
    boolean_words: dict[str, tuple[str, str]]


def _error_decl() -> dict[str, Any]:
    # The screen-``data`` declaration of a field's ``__err`` (its error-message text) or ``__invalid``.
    return {"type": "string", "__example__": ERROR_EXAMPLE}


def _range_error_decl() -> dict[str, Any]:
    # The screen-``data`` declaration of a merged range START's ``__err``: the one object the range
    # control's ``error-message`` binds, carrying each end's error text.
    return {
        "type": "object",
        "properties": {"start-date": {"type": "string"}, "end-date": {"type": "string"}},
        "__example__": {"start-date": ERROR_EXAMPLE, "end-date": ERROR_EXAMPLE},
    }


def screen_data_model(
    index: int,
    this_fields: list[str],
    later_fields: list[str],
    earlier_fields: list[str],
    pages: list[dict[str, Any]],
    spec: RenderSpec,
) -> dict[str, Any]:
    """The ``data`` declarations of screen ``index`` of the form's resolved ``pages``.

    The entry screen declares EVERY field's init/ds/err (the send injects them all there); a later screen
    declares its own and its successors' init/ds/err plus a ``__val`` carrier for each field collected
    earlier. A merged range START's ``__err`` is the pair's one error object (:func:`_range_error_decl`)
    and a merged range END declares nothing. Every screen declares, as a string, the display slots of
    its own page and of every later page (the send injects them all at the entry screen).
    Every screen of an ask-less endpoint-driven form declares each shaped field's ``__invalid``, each
    field's ``__required`` the form-shape check can report missing, the form-line carriers and every
    date field's bound carriers.
    """
    merged_ends = set(spec.range_merges)
    merged_starts = set(spec.range_merges.values())
    data_model: dict[str, Any] = {}
    rendered = list(spec.properties) if index == 0 else [*this_fields, *later_fields]
    for name in rendered:
        if name not in merged_ends:
            data_model.update(
                field_data_decls(name, spec.properties[name], spec.option_fields, spec.names, _range_end(name, spec))
            )
        if name in spec.err_fields and name not in merged_ends:
            data_model[f"{spec.names[name]}__err"] = _range_error_decl() if name in merged_starts else _error_decl()
    if index > 0:
        for name in earlier_fields:
            if name in merged_ends:
                continue
            data_model[f"{spec.names[name]}__val"] = (
                range_value_type_decl() if name in merged_starts else value_type_decl(spec.properties[name])
            )
    data_model.update(_every_screen_decls(spec))
    for key in slot_keys(pages[index:], spec.slots):
        data_model[key] = {"type": "string", "__example__": ""}
    return data_model


def _range_end(name: str, spec: RenderSpec) -> dict[str, Any] | None:
    # The END's definition when ``name`` is a merged range START, else ``None``.
    end = next((end for end, start in spec.range_merges.items() if start == name), None)
    return spec.properties[end] if end is not None else None


def _every_screen_decls(spec: RenderSpec) -> dict[str, Any]:
    # The carriers an ask-less endpoint-driven form declares on every screen: each shaped field's
    # ``__invalid``, each reportable-missing field's ``__required``, the form-line carriers and every
    # date field's bound carriers.
    if not spec.askless_endpoint:
        return {}
    decls: dict[str, Any] = {}
    shaped = shaped_fields(spec.properties)
    reportable_missing = required_fields(spec.properties, spec.required)
    for name in spec.properties:
        if name in shaped:
            decls[f"{spec.names[name]}__invalid"] = _error_decl()
        if name in reportable_missing:
            decls[f"{spec.names[name]}__required"] = _error_decl()
    for member in FORM_LINE_MEMBERS:
        decls[form_line_carrier(member)] = _error_decl()
    for name, end in date_field_ends(list(spec.properties), spec.properties, spec.range_merges):
        end_prop = spec.properties[end] if end is not None else None
        decls.update(date_bound_decls(spec.names[name], spec.properties[name], end_prop))
    return decls


def later_carriers(later_fields: list[str], later_pages: list[dict[str, Any]], spec: RenderSpec) -> dict[str, str]:
    """The carriers a later screen still needs, forwarded from this screen's data.

    Each later (unrendered) field's init/ds, bound carriers and ``__err`` (a merged START's the
    pair's, a merged END none), and the display slot of each block on ``later_pages``.
    """
    merged_ends = set(spec.range_merges)
    forward: dict[str, str] = {}
    for name in later_fields:
        if name not in merged_ends:
            forward.update(
                forward_field_data(name, spec.properties[name], spec.option_fields, spec.names, _range_end(name, spec))
            )
        if name in spec.err_fields and name not in merged_ends:
            forward[f"{spec.names[name]}__err"] = f"${{data.{spec.names[name]}__err}}"
    forward.update({key: f"${{data.{key}}}" for key in slot_keys(later_pages, spec.slots)})
    return forward
