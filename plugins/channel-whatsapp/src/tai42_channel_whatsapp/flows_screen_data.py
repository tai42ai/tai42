"""The render spec every Flow screen reads, and the screen-``data`` block it yields.

:class:`_RenderSpec` carries the cross-cutting inputs of one Flow build. From it each screen
declares its ``data`` model (:func:`_screen_data_model`): every field's init/ds/err, the ``__val``
carriers of fields collected earlier, and the caller-word carriers an ask-less endpoint-driven form
rides; and forwards the carriers its later fields still need (:func:`_later_carriers`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tai42_channel_whatsapp.flows_components import (
    _field_data_decls,
    _forward_field_data,
    _range_value_type_decl,
    _value_type_decl,
)
from tai42_channel_whatsapp.flows_endpoint import FORM_LINE_MEMBERS, form_line_carrier
from tai42_channel_whatsapp.flows_shapes import ERROR_EXAMPLE


@dataclass(frozen=True)
class _RenderSpec:
    """The cross-cutting inputs every screen reads while a Flow is built.

    ``field_changed`` are the schema keys whose change fires a reaction; ``page_advanced`` the
    page TITLES whose advance fires one; ``submitted`` whether the submission is reaction-checked.
    ``endpoint`` is True for an endpoint-driven Flow (a reacting form, or one carrying a shape no
    control checks natively) and drives the ``data_exchange`` footers + ``data_api_version``.
    ``slots`` maps each display slot to its screen-``data`` key. ``field_invalid`` is the caller's
    per-field ``invalid`` sentence; ``err_fields`` the fields binding an ``error-message``;
    ``invalid_fields`` the shaped fields whose sentence rides the Flow as ``__invalid`` and
    ``required_fields`` the fields whose ``required`` sentence rides it as ``__required`` (an ask-less
    endpoint-driven form only, else empty); ``form_lines`` whether the caller's form-level lines ride it
    under their form-line carriers (the same forms). ``boolean_words`` maps a boolean field to the
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
    invalid_fields: frozenset[str]
    required_fields: frozenset[str]
    form_lines: bool
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


def _screen_data_model(
    index: int, this_fields: list[str], later_fields: list[str], earlier_fields: list[str], spec: _RenderSpec
) -> dict[str, Any]:
    """The screen's ``data`` declarations.

    The entry screen declares EVERY field's init/ds/err (the send injects them all there); a later screen
    declares its own and its successors' init/ds/err plus a ``__val`` carrier for each field collected
    earlier. A merged range START's ``__err`` is the pair's one error object (:func:`_range_error_decl`)
    and a merged range END declares nothing.
    Every screen of an ask-less endpoint-driven form declares each shaped field's ``__invalid``, each
    field's ``__required`` the form-shape check can report missing, and the form-line carriers.
    """
    merged_ends = set(spec.range_merges)
    merged_starts = set(spec.range_merges.values())
    data_model: dict[str, Any] = {}
    rendered = list(spec.properties) if index == 0 else [*this_fields, *later_fields]
    for name in rendered:
        if name not in merged_ends:
            data_model.update(
                _field_data_decls(name, spec.properties[name], spec.option_fields, spec.names, name in merged_starts)
            )
        if name in spec.err_fields and name not in merged_ends:
            data_model[f"{spec.names[name]}__err"] = _range_error_decl() if name in merged_starts else _error_decl()
    if index > 0:
        for name in earlier_fields:
            if name in merged_ends:
                continue
            data_model[f"{spec.names[name]}__val"] = (
                _range_value_type_decl() if name in merged_starts else _value_type_decl(spec.properties[name])
            )
    data_model.update(_caller_word_decls(spec))
    return data_model


def _caller_word_decls(spec: _RenderSpec) -> dict[str, Any]:
    # The caller-word carriers an ask-less endpoint-driven form declares on every screen: each shaped
    # field's ``__invalid``, each reportable-missing field's ``__required`` and the form-line carriers.
    decls: dict[str, Any] = {}
    for name in spec.properties:
        if name in spec.invalid_fields:
            decls[f"{spec.names[name]}__invalid"] = _error_decl()
        if name in spec.required_fields:
            decls[f"{spec.names[name]}__required"] = _error_decl()
    if spec.form_lines:
        for member in FORM_LINE_MEMBERS:
            decls[form_line_carrier(member)] = _error_decl()
    return decls


def _later_carriers(later_fields: list[str], spec: _RenderSpec) -> dict[str, str]:
    # The carriers each later (unrendered) field still needs on a later screen: its init/ds and its
    # ``__err`` (a merged START's the pair's one error object, a merged END none), forwarded from
    # this screen's data.
    merged_ends = set(spec.range_merges)
    forward: dict[str, str] = {}
    for name in later_fields:
        if name not in merged_ends:
            forward.update(_forward_field_data(name, spec.properties[name], spec.option_fields, spec.names))
        if name in spec.err_fields and name not in merged_ends:
            forward[f"{spec.names[name]}__err"] = f"${{data.{spec.names[name]}__err}}"
    return forward
