"""The declared shapes on a WhatsApp Flow form: what Meta's controls check natively, what makes a form
endpoint-driven, the per-field ``error-message`` bindings and the channel's own words."""

from __future__ import annotations

import re
from typing import Any

import pytest
from tai42_contract.channels import ChannelInputError
from tai42_contract.interactions import FormShapeKind
from tai42_kit.interactions import FORMAT_PATTERNS

from tai42_channel_whatsapp.flows import build_flow_data, build_form_flow, form_needs_endpoint
from tai42_channel_whatsapp.flows_shapes import ERROR_MESSAGE_MAX_CHARS, channel_word, helper_word


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required is not None:
        schema["required"] = required
    return schema


def _controls(flow_json: dict, index: int = 0) -> list[dict]:
    return [child for child in flow_json["screens"][index]["layout"]["children"] if child["type"] != "Footer"]


def _footer(flow_json: dict, index: int = 0) -> dict:
    return next(child for child in flow_json["screens"][index]["layout"]["children"] if child["type"] == "Footer")


def _control(flow_json: dict, name: str, index: int = 0) -> dict:
    return next(child for child in _controls(flow_json, index) if child.get("name") == name)


# -- native shapes drawn on the control ----------------------------------------


def test_a_pattern_on_a_text_input_is_drawn_with_its_description_as_helper_text():
    flow, _ = build_form_flow(
        _schema({"code": {"type": "string", "pattern": "^[0-9]{4}$", "description": "Four digits"}})
    )
    control = _control(flow, "code")
    assert control["pattern"] == "^[0-9]{4}$"
    assert control["helper-text"] == "Four digits"


def test_a_pattern_without_a_description_draws_the_callers_sentence_else_the_channel_word():
    schema = _schema({"code": {"type": "string", "pattern": "^[0-9]{4}$"}})
    assert _control(build_form_flow(schema)[0], "code")["helper-text"] == channel_word(FormShapeKind.PATTERN, {})
    flow, _ = build_form_flow(schema, field_invalid={"code": "Four digits, please"})
    assert _control(flow, "code")["helper-text"] == "Four digits, please"


def test_a_pattern_on_a_numeric_keyboard_is_drawn():
    flow, _ = build_form_flow(_schema({"pin": {"type": "string", "pattern": "^[0-9]+$", "inputMode": "numeric"}}))
    control = _control(flow, "pin")
    assert control["input-type"] == "number"
    assert control["pattern"] == "^[0-9]+$"


@pytest.mark.parametrize("mode", ["email", "tel"])
def test_a_pattern_on_an_email_or_phone_keyboard_keeps_the_keyboard_and_is_not_drawn(mode: str):
    flow, _ = build_form_flow(_schema({"v": {"type": "string", "pattern": "x", "inputMode": mode}}))
    control = _control(flow, "v")
    assert control["input-type"] == ("email" if mode == "email" else "phone")
    assert "pattern" not in control


def test_an_integer_draws_the_whole_number_pattern():
    flow, _ = build_form_flow(_schema({"n": {"type": "integer"}}))
    control = _control(flow, "n")
    assert control["input-type"] == "number"
    assert control["pattern"] == "^-?[0-9]+$"
    assert control["helper-text"] == "A whole number, like 42"


def test_a_number_draws_no_pattern():
    assert "pattern" not in _control(build_form_flow(_schema({"n": {"type": "number"}}))[0], "n")


@pytest.mark.parametrize(
    ("fmt", "pattern"),
    [
        ("time", "^[0-9]{2}:[0-9]{2}(:[0-9]{2})?$"),
        (
            "date-time",
            "^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})$",
        ),
    ],
)
def test_time_and_date_time_draw_the_platform_regex(fmt: str, pattern: str):
    control = _control(build_form_flow(_schema({"at": {"type": "string", "format": fmt}}))[0], "at")
    assert control["type"] == "TextInput"
    assert control["pattern"] == pattern
    assert pattern == FORMAT_PATTERNS[fmt]


@pytest.mark.parametrize(
    ("fmt", "helper", "example"),
    [
        ("time", "A time as HH:MM, like 09:30", "09:30"),
        ("date-time", "Date, time and zone, like 2030-12-31T09:30:00+01:00", "2030-12-31T09:30:00+01:00"),
    ],
)
def test_time_and_date_time_without_a_description_show_the_expected_form_with_an_example(
    fmt: str, helper: str, example: str
):
    # The helper text names the form the control's pattern wants, and its example passes that pattern.
    control = _control(build_form_flow(_schema({"at": {"type": "string", "format": fmt}}))[0], "at")
    assert control["helper-text"] == helper
    assert re.fullmatch(control["pattern"], example)


def test_an_email_format_draws_the_email_keyboard_and_no_pattern():
    control = _control(build_form_flow(_schema({"e": {"type": "string", "format": "email"}}))[0], "e")
    assert control["input-type"] == "email"
    assert "pattern" not in control


def test_every_date_draws_a_single_calendar_picker_carrying_required():
    flow, _ = build_form_flow(_schema({"on": {"type": "string", "format": "date", "title": "On"}}, ["on"]))
    assert _control(flow, "on") == {
        "type": "CalendarPicker",
        "name": "on",
        "label": "On",
        "mode": "single",
        "required": True,
        "init-value": "${data.on__init}",
        "error-message": "${data.on__err}",
    }


# -- the per-field error-message binding ----------------------------------------


_EVERY_CONTROL = _schema(
    {
        "name": {"type": "string"},
        "notes": {"type": "string", "control": "textarea"},
        "size": {"type": "string", "enum": ["s", "m"]},
        "colour": {"type": "string", "enum": ["a", "b", "c", "d", "e", "f"]},
        "tags": {"type": "array", "items": {"type": "string", "enum": ["x", "y"]}},
        "on": {"type": "string", "format": "date"},
        "agree": {"type": "boolean"},
        "chips": {"type": "string", "enum": ["p", "q"], "control": "chips"},
    }
)


def test_every_control_that_shows_an_error_binds_its_error_message():
    flow, _ = build_form_flow(_EVERY_CONTROL)
    for name in ("name", "notes", "size", "colour", "tags", "on"):
        assert _control(flow, name)["error-message"] == f"${{data.{name}__err}}", name
    # Meta's OptIn and ChipsSelector take no error-message; their errors ride the screen snackbar.
    assert "error-message" not in _control(flow, "agree")
    assert "error-message" not in _control(flow, "chips")
    data = flow["screens"][0]["data"]
    for name in ("name", "notes", "size", "colour", "tags", "on"):
        assert data[f"{name}__err"] == {"type": "string", "__example__": "Not an accepted value"}
    assert "agree__err" not in data


def test_the_send_data_clears_every_error_binding():
    data = build_flow_data(_EVERY_CONTROL, {}, {})
    assert {key: value for key, value in data.items() if key.endswith("__err")} == {
        f"{name}__err": "" for name in ("name", "notes", "size", "colour", "tags", "on")
    }


def test_a_merged_range_binds_both_ends_error_message():
    schema = _schema(
        {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start", "maxDays": 5},
        }
    )
    flow, _ = build_form_flow(schema)
    control = _control(flow, "start")
    assert control["mode"] == "range"
    assert control["error-message"] == {"start-date": "${data.start__err}", "end-date": "${data.end__err}"}
    assert {"start__err", "end__err"} <= set(flow["screens"][0]["data"])
    assert {"start__err", "end__err"} <= set(build_flow_data(schema, {}, {}))


_PAGES = [
    {"title": "One", "fields": ["name"]},
    {"title": "Two", "fields": ["email"]},
    {"title": "Three", "fields": ["qty"]},
]
_STEPPED = _schema(
    {
        "name": {"type": "string"},
        "email": {"type": "string", "format": "email"},
        "qty": {"type": "integer", "minimum": 1},
    }
)


def test_each_step_declares_and_forwards_the_later_error_bindings():
    flow, _ = build_form_flow(_STEPPED, _PAGES)
    assert {"name__err", "email__err", "qty__err"} <= set(flow["screens"][0]["data"])
    assert {"email__err", "qty__err"} <= set(flow["screens"][1]["data"])
    assert "name__err" not in flow["screens"][1]["data"]
    payload = _footer(flow, 0)["on-click-action"]["payload"]
    assert payload["email__err"] == "${data.email__err}"
    assert payload["qty__err"] == "${data.qty__err}"


# -- endpoint-driven: shapes Meta cannot check ----------------------------------


@pytest.mark.parametrize(
    ("prop", "endpoint"),
    [
        ({"type": "string"}, False),
        ({"type": "string", "pattern": "^a"}, False),
        ({"type": "string", "pattern": "^a", "inputMode": "numeric"}, False),
        ({"type": "string", "pattern": "^a", "inputMode": "email"}, True),
        ({"type": "string", "pattern": "^a", "inputMode": "tel"}, True),
        ({"type": "string", "pattern": "^a", "control": "textarea"}, True),
        ({"type": "string", "minLength": 2, "maxLength": 9}, False),
        ({"type": "string", "maxLength": 9, "control": "textarea"}, False),
        ({"type": "string", "minLength": 2, "control": "textarea"}, True),
        ({"type": "string", "format": "email"}, True),
        ({"type": "string", "format": "time"}, True),
        ({"type": "string", "format": "date-time"}, True),
        ({"type": "string", "format": "date", "minDate": "2026-01-01", "unavailableDates": ["sunday"]}, False),
        ({"type": "integer"}, False),
        ({"type": "integer", "minimum": 1}, True),
        ({"type": "number", "maximum": 9}, True),
        ({"type": "number", "exclusiveMinimum": 0}, True),
        ({"type": "integer", "multipleOf": 2}, True),
        ({"type": "string", "enum": ["a", "b"]}, False),
        ({"type": "array", "items": {"type": "string", "enum": ["a", "b"]}, "minItems": 1, "maxItems": 2}, False),
        ({"type": "array", "items": {"type": "string", "enum": ["a", "b"]}, "uniqueItems": True}, True),
        ({"type": "boolean", "title": "Agree", "description": "d", "default": False}, False),
        ({"type": "string", "const": "x"}, True),
    ],
)
def test_needs_endpoint_truth_table(prop: dict[str, Any], endpoint: bool):
    assert form_needs_endpoint(_schema({"f": prop})) is endpoint


def test_a_range_merged_into_one_control_is_native_and_a_split_one_is_not():
    pair = {
        "start": {"type": "string", "format": "date"},
        "end": {"type": "string", "format": "date", "rangeStart": "start", "minDays": 2},
    }
    assert form_needs_endpoint(_schema(pair)) is False
    split = [{"title": "A", "fields": ["start"]}, {"title": "B", "fields": ["end"]}]
    assert form_needs_endpoint(_schema(pair), split) is True


def test_a_reacting_form_is_endpoint_driven():
    schema = _schema({"size": {"type": "string", "enum": ["s", "m"]}})
    assert form_needs_endpoint(schema, reactions={"field_changed": ["size"]}) is True
    assert form_needs_endpoint(schema) is False


def test_a_shape_checked_form_routes_every_footer_through_the_endpoint():
    flow, _ = build_form_flow(_STEPPED, _PAGES)
    assert flow["data_api_version"] == "3.0"
    assert flow["routing_model"] == {"SCREEN_A": ["SCREEN_B"], "SCREEN_B": ["SCREEN_C"], "SCREEN_C": []}
    first = _footer(flow, 0)["on-click-action"]
    assert first["name"] == "data_exchange"
    assert first["payload"]["tai42_event"] == "page_advanced"
    assert first["payload"]["tai42_page"] == "One"
    assert first["payload"]["name"] == "${form.name}"
    # The later fields' carriers ride the payload, so the endpoint builds the next screen from it.
    assert first["payload"]["email__init"] == "${data.email__init}"
    second = _footer(flow, 1)["on-click-action"]["payload"]
    assert second["name"] == "${data.name__val}"
    assert second["email"] == "${form.email}"
    last = _footer(flow, 2)["on-click-action"]
    assert last["name"] == "data_exchange"
    assert last["payload"] == {
        "tai42_event": "submitted",
        "qty": "${form.qty}",
        "name": "${data.name__val}",
        "email": "${data.email__val}",
        "qty__init": "${data.qty__init}",
        "qty__err": "${data.qty__err}",
        "name__val": "${data.name__val}",
        "email__val": "${data.email__val}",
    }


def test_a_notify_form_carries_each_shaped_fields_sentence_through_the_flow():
    flow, _ = build_form_flow(_STEPPED, _PAGES, carry_invalid=True)
    for index in range(3):
        assert {"email__invalid", "qty__invalid"} <= set(flow["screens"][index]["data"]), index
        assert "name__invalid" not in flow["screens"][index]["data"]
    step = _footer(flow, 0)["on-click-action"]["payload"]
    assert step["email__invalid"] == "${data.email__invalid}"
    assert step["qty__invalid"] == "${data.qty__invalid}"
    assert _footer(flow, 2)["on-click-action"]["payload"]["qty__invalid"] == "${data.qty__invalid}"
    data = build_flow_data(_STEPPED, {}, {}, _PAGES, field_invalid={"qty": "One or more"}, carry_invalid=True)
    assert data["qty__invalid"] == "One or more"
    assert data["email__invalid"] == ""


def test_an_endpoint_driven_form_refuses_a_property_named_like_a_carrier():
    schema = _schema({"a": {"type": "string", "format": "email"}, "a__init": {"type": "string"}})
    with pytest.raises(ChannelInputError, match="collide"):
        build_form_flow(schema)


_REACTING = _schema(
    {
        "size": {"type": "string", "enum": ["s", "m"]},
        "name": {"type": "string"},
        "email": {"type": "string", "format": "email"},
    }
)
_REACTING_PAGES = [
    {"title": "One", "fields": ["size", "name"], "display": [{"kind": "body", "slot": "total"}]},
    {"title": "Two", "fields": ["email"], "display": [{"kind": "body", "slot": "summary"}]},
]


def test_every_data_exchange_action_carries_its_screens_whole_data():
    # The data an endpoint answers with is the screen's state from then on, so every action that
    # reaches the endpoint carries the screen's whole data for the answer to start from.
    flow, _ = build_form_flow(_REACTING, _REACTING_PAGES, reactions={"field_changed": ["size"]}, carry_invalid=True)
    selects = 0
    for index, screen in enumerate(flow["screens"]):
        whole = {key: f"${{data.{key}}}" for key in screen["data"]}
        actions = [_footer(flow, index)["on-click-action"]]
        for control in _controls(flow, index):
            if "on-select-action" in control:
                actions.append(control["on-select-action"])
                selects += 1
        for action in actions:
            assert whole.items() <= action["payload"].items(), (index, action["payload"])
    assert selects == 1


def test_the_send_data_supplies_the_entry_screens_display_slots_empty():
    data = build_flow_data(_REACTING, {}, {}, _REACTING_PAGES)
    assert data["slot_total"] == ""
    assert "slot_summary" not in data


def test_an_endpoint_driven_form_refuses_a_property_named_like_a_display_slot():
    schema = _schema({"a": {"type": "string", "format": "email"}, "slot_total": {"type": "string"}})
    pages = [{"title": "One", "fields": ["a", "slot_total"], "display": [{"kind": "body", "slot": "total"}]}]
    with pytest.raises(ChannelInputError, match="collide"):
        build_form_flow(schema, pages)


def test_a_static_form_keeps_its_navigate_flow():
    schema = _schema(
        {"name": {"type": "string", "title": "Name"}, "on": {"type": "string", "format": "date"}}, ["name"]
    )
    flow, _ = build_form_flow(schema)
    assert flow == {
        "version": "7.0",
        "screens": [
            {
                "id": "SCREEN_A",
                "title": "Form",
                "terminal": True,
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {
                            "type": "TextInput",
                            "name": "name",
                            "label": "Name",
                            "required": True,
                            "init-value": "${data.name__init}",
                            "error-message": "${data.name__err}",
                        },
                        {
                            "type": "CalendarPicker",
                            "name": "on",
                            "label": "on",
                            "mode": "single",
                            "required": False,
                            "init-value": "${data.on__init}",
                            "error-message": "${data.on__err}",
                        },
                        {
                            "type": "Footer",
                            "label": "Submit",
                            "on-click-action": {
                                "name": "complete",
                                "payload": {"Name": "${form.name}", "on": "${form.on}"},
                            },
                        },
                    ],
                },
                "data": {
                    "name__init": {"type": "string", "__example__": ""},
                    "name__err": {"type": "string", "__example__": "Not an accepted value"},
                    "on__init": {"type": "string", "__example__": ""},
                    "on__err": {"type": "string", "__example__": "Not an accepted value"},
                },
            }
        ],
    }


# -- the channel's own words ------------------------------------------------------


_WORD_PARAMS = [{}, *({"format": fmt} for fmt in FORMAT_PATTERNS), *({"type": t} for t in ("integer", "number"))]


@pytest.mark.parametrize("params", _WORD_PARAMS)
@pytest.mark.parametrize("kind", list(FormShapeKind))
def test_every_kind_has_a_channel_word_within_the_text_error_cap(kind: FormShapeKind, params: dict[str, Any]):
    word = channel_word(kind, params)
    assert word
    assert len(word) <= ERROR_MESSAGE_MAX_CHARS["TextInput"]


@pytest.mark.parametrize("params", _WORD_PARAMS)
@pytest.mark.parametrize("kind", list(FormShapeKind))
def test_every_kind_has_a_helper_word_within_the_helper_text_cap(kind: FormShapeKind, params: dict[str, Any]):
    word = helper_word(kind, params)
    assert word
    assert len(word) <= 80


@pytest.mark.parametrize(
    ("fmt", "word"),
    [
        ("email", "Use name@example.com"),
        ("date", "Use YYYY-MM-DD"),
        ("time", "Use HH:MM"),
        ("date-time", "Use YYYY-MM-DDTHH:MM:SS+HH:MM"),
    ],
)
def test_a_format_word_names_the_expected_form(fmt: str, word: str):
    assert channel_word(FormShapeKind.FORMAT, {"format": fmt}) == word


@pytest.mark.parametrize(("json_type", "word"), [("integer", "Enter a whole number"), ("number", "Enter a number")])
def test_a_type_word_names_the_expected_kind(json_type: str, word: str):
    assert channel_word(FormShapeKind.TYPE, {"type": json_type}) == word
