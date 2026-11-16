"""A date field's bounds on WhatsApp: the published Flow binds which bounds a field has, each send carries their values.

``minDate``/``maxDate``/``unavailableDates`` draw as ``CalendarPicker`` bindings to screen-data carriers
(``<name>__min``, ``__max``, ``__unavailable``, ``__days``); ``build_flow_data`` fills them per send; the
publish key leaves the values out, so forms differing only in them share one published Flow."""

from __future__ import annotations

import pytest
from tai42_contract.channels import ChannelInputError

from tai42_channel_whatsapp.flows import build_flow_data, build_form_flow

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_BOUND_MEMBERS = ("min-date", "max-date", "unavailable-dates", "include-days")
_BOUND_CARRIERS = ("when__min", "when__max", "when__unavailable", "when__days")


def _schema(when: dict, **others: dict) -> dict:
    return {
        "type": "object",
        "properties": {"label": {"type": "string"}, "when": {"type": "string", "format": "date", **when}, **others},
        "required": ["when"],
    }


_BOUNDED = {"minDate": "2026-01-01", "maxDate": "2026-12-31", "unavailableDates": ["2026-07-04", "saturday", "sunday"]}


def _controls(flow_json: dict, index: int = 0) -> list[dict]:
    children = flow_json["screens"][index]["layout"]["children"]
    return [child for child in children if child["type"] != "Footer"]


def _control(flow_json: dict, name: str, index: int = 0) -> dict:
    return next(control for control in _controls(flow_json, index) if control.get("name") == name)


def _footer_payload(flow_json: dict, index: int) -> dict:
    footer = flow_json["screens"][index]["layout"]["children"][-1]
    return footer["on-click-action"]["payload"]


# --- the published Flow binds the bounds a field has ------------------------------------------


def test_a_bounded_date_declares_each_bound_carrier_on_the_entry_screen():
    flow_json, _ = build_form_flow(_schema(_BOUNDED))
    data = flow_json["screens"][0]["data"]
    assert data["when__min"] == {"type": "string", "__example__": ""}
    assert data["when__max"] == {"type": "string", "__example__": ""}
    assert data["when__unavailable"] == {"type": "array", "items": {"type": "string"}, "__example__": []}
    assert data["when__days"] == {"type": "array", "items": {"type": "string"}, "__example__": []}


def test_a_date_without_bounds_binds_none():
    flow_json, _ = build_form_flow(_schema({}))
    picker = _control(flow_json, "when")
    assert not set(_BOUND_MEMBERS) & set(picker)
    assert not set(_BOUND_CARRIERS) & set(flow_json["screens"][0]["data"])


def test_a_date_with_only_a_min_date_binds_only_the_min_date():
    flow_json, _ = build_form_flow(_schema({"minDate": "2026-03-01"}))
    picker = _control(flow_json, "when")
    assert {member for member in _BOUND_MEMBERS if member in picker} == {"min-date"}
    assert {key for key in _BOUND_CARRIERS if key in flow_json["screens"][0]["data"]} == {"when__min"}
    assert build_flow_data(_schema({"minDate": "2026-03-01"}), {}, {})["when__min"] == "2026-03-01"
    assert not {"when__max", "when__unavailable", "when__days"} & set(
        build_flow_data(_schema({"minDate": "2026-03-01"}), {}, {})
    )


def test_a_later_screens_date_has_its_bound_carriers_forwarded_by_the_navigate_payload():
    pages = [{"title": "One", "fields": ["label"]}, {"title": "Two", "fields": ["when"]}]
    flow_json, _ = build_form_flow(_schema(_BOUNDED), pages)
    assert "data_api_version" not in flow_json
    forward = _footer_payload(flow_json, 0)
    for key in _BOUND_CARRIERS:
        assert forward[key] == f"${{data.{key}}}"
        assert key in flow_json["screens"][1]["data"]
    assert _control(flow_json, "when", 1)["min-date"] == "${data.when__min}"


_PAIR_PAGES = [{"title": "One", "fields": ["label"]}, {"title": "Two", "fields": ["start", "end"]}]


def _pair_schema(start: dict, end: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "start": {"type": "string", "format": "date", "title": "From", **start},
            "end": {"type": "string", "format": "date", "rangeStart": "start", "title": "To", **end},
        },
        "required": ["start", "end"],
    }


def test_a_merged_pair_on_a_later_screen_binds_declares_and_forwards_a_bound_only_its_end_declares():
    flow_json, _ = build_form_flow(_pair_schema({}, {"minDate": "2026-04-01"}), _PAIR_PAGES)
    picker = _control(flow_json, "start", 1)
    assert picker["mode"] == "range"
    assert picker["min-date"] == "${data.start__min}"
    assert "max-date" not in picker
    assert "start__min" in flow_json["screens"][1]["data"]
    assert "end__min" not in flow_json["screens"][1]["data"]
    assert _footer_payload(flow_json, 0)["start__min"] == "${data.start__min}"
    assert (
        build_flow_data(_pair_schema({}, {"minDate": "2026-04-01"}), {}, {}, _PAIR_PAGES)["start__min"] == "2026-04-01"
    )


def test_an_ask_less_endpoint_form_declares_every_date_fields_bound_carriers_on_every_screen():
    schema = _schema(_BOUNDED, email={"type": "string", "format": "email"})
    pages = [{"title": "One", "fields": ["when"]}, {"title": "Two", "fields": ["label", "email"]}]
    flow_json, _ = build_form_flow(schema, pages, askless=True)
    assert flow_json["data_api_version"] == "3.0"
    for index in range(2):
        assert set(_BOUND_CARRIERS) <= set(flow_json["screens"][index]["data"]), index
    asked, _ = build_form_flow(schema, pages)
    assert not set(_BOUND_CARRIERS) & set(asked["screens"][1]["data"])
    data = build_flow_data(schema, {}, {}, pages, askless=True)
    assert data["when__min"] == "2026-01-01"
    assert data["when__days"] == _WEEKDAYS[:5]


# --- each send carries the values ---------------------------------------------------------------


def test_the_send_carries_each_bound_value():
    data = build_flow_data(_schema(_BOUNDED), {}, {})
    assert data["when__min"] == "2026-01-01"
    assert data["when__max"] == "2026-12-31"
    assert data["when__unavailable"] == ["2026-07-04"]
    assert data["when__days"] == _WEEKDAYS[:5]


def test_only_excluded_weekdays_send_no_dates_and_the_kept_days():
    data = build_flow_data(_schema({"unavailableDates": ["saturday", "sunday"]}), {}, {})
    assert data["when__unavailable"] == []
    assert data["when__days"] == _WEEKDAYS[:5]


def test_only_excluded_dates_keep_all_seven_days():
    data = build_flow_data(_schema({"unavailableDates": ["2026-07-04", "2026-12-25"]}), {}, {})
    assert data["when__unavailable"] == ["2026-07-04", "2026-12-25"]
    assert data["when__days"] == _WEEKDAYS


def test_a_merged_start_carries_bounds_never_looser_than_either_end():
    schema = _pair_schema(
        {"minDate": "2026-01-01", "maxDate": "2026-06-30", "unavailableDates": ["2026-02-02", "saturday"]},
        {"minDate": "2026-02-01", "maxDate": "2026-12-31", "unavailableDates": ["2026-03-03", "sunday"]},
    )
    data = build_flow_data(schema, {}, {})
    assert data["start__min"] == "2026-02-01"
    assert data["start__max"] == "2026-06-30"
    assert data["start__unavailable"] == ["2026-02-02", "2026-03-03"]
    assert data["start__days"] == _WEEKDAYS[:5]
    assert not {"end__min", "end__max", "end__unavailable", "end__days"} & set(data)


def test_a_merged_pair_whose_merged_bounds_leave_no_date_is_refused_naming_the_pair():
    # The one range control would offer no selectable date: its merged ``minDate`` (the END's) is
    # later than its merged ``maxDate`` (the START's).
    schema = _pair_schema({"maxDate": "2026-02-01"}, {"minDate": "2026-03-01"})
    with pytest.raises(ChannelInputError, match=r"'start'.*'end'.*2026-03-01.*2026-02-01"):
        build_flow_data(schema, {}, {})


# --- the publish key leaves the values out --------------------------------------------------------


def test_forms_differing_only_in_a_bound_value_share_one_key():
    _, key = build_form_flow(_schema(_BOUNDED))
    for changed in (
        {**_BOUNDED, "minDate": "2026-02-01"},
        {**_BOUNDED, "maxDate": "2026-11-30"},
        {**_BOUNDED, "unavailableDates": ["2026-08-01", "monday"]},
    ):
        assert build_form_flow(_schema(changed))[1] == key


def test_a_field_gaining_a_bound_keys_a_different_flow():
    assert build_form_flow(_schema({"minDate": "2026-01-01"}))[1] != build_form_flow(_schema({}))[1]


def test_the_schema_without_bound_values_keeps_everything_else():
    from tai42_channel_whatsapp.flows_dates import schema_without_date_bound_values

    schema = _schema(_BOUNDED)
    assert schema_without_date_bound_values(schema) == _schema({})
    assert schema["properties"]["when"]["minDate"] == "2026-01-01"


# --- a date-time field: no control binds its bounds; they ride only where the data endpoint reads them ---

_AT_CARRIERS = ("at__min", "at__max", "at__unavailable", "at__days")


def _at_schema(at: dict) -> dict:
    return {
        "type": "object",
        "properties": {"label": {"type": "string"}, "at": {"type": "string", "format": "date-time", **at}},
        "required": ["at"],
    }


def test_a_date_time_field_differing_only_in_a_bound_value_shares_one_key():
    _, key = build_form_flow(_at_schema(_BOUNDED))
    for changed in (
        {**_BOUNDED, "minDate": "2026-02-01"},
        {**_BOUNDED, "maxDate": "2026-11-30"},
        {**_BOUNDED, "unavailableDates": ["2026-08-01", "monday"]},
    ):
        assert build_form_flow(_at_schema(changed))[1] == key


def test_which_bounds_a_date_time_field_has_still_keys_an_ask_less_flow():
    def key(at: dict) -> str:
        return build_form_flow(_at_schema(at), askless=True)[1]

    assert key({"minDate": "2026-01-01"}) != key({})
    assert key({"minDate": "2026-01-01"}) != key({"maxDate": "2026-01-01"})


def test_a_date_time_field_declares_and_sends_its_bound_carriers_only_in_an_ask_less_form():
    # An ask's data endpoint checks the bounds from the ask's own schema and no control binds them,
    # so an ask carries none; an ask-less form's data endpoint reads them from the request.
    asked, _ = build_form_flow(_at_schema(_BOUNDED))
    control = _control(asked, "at")
    assert control["type"] == "TextInput"
    assert not set(_BOUND_MEMBERS) & set(control)
    assert not set(_AT_CARRIERS) & set(asked["screens"][0]["data"])
    assert not set(_AT_CARRIERS) & set(build_flow_data(_at_schema(_BOUNDED), {}, {}))

    askless, _ = build_form_flow(_at_schema(_BOUNDED), askless=True)
    data = askless["screens"][0]["data"]
    assert data["at__min"] == {"type": "string", "__example__": ""}
    assert data["at__unavailable"] == {"type": "array", "items": {"type": "string"}, "__example__": []}
    sent = build_flow_data(_at_schema(_BOUNDED), {}, {}, askless=True)
    assert sent["at__min"] == "2026-01-01"
    assert sent["at__max"] == "2026-12-31"
    assert sent["at__unavailable"] == ["2026-07-04"]
    assert sent["at__days"] == _WEEKDAYS[:5]


def test_a_date_time_field_without_bounds_carries_none():
    flow_json, _ = build_form_flow(_at_schema({}), askless=True)
    assert not set(_AT_CARRIERS) & set(flow_json["screens"][0]["data"])
    assert not set(_AT_CARRIERS) & set(build_flow_data(_at_schema({}), {}, {}, askless=True))


def test_an_ask_less_form_declares_a_date_time_fields_bound_carriers_on_every_screen():
    pages = [{"title": "One", "fields": ["at"]}, {"title": "Two", "fields": ["label"]}]
    flow_json, _ = build_form_flow(_at_schema(_BOUNDED), pages, askless=True)
    for index in range(2):
        assert set(_AT_CARRIERS) <= set(flow_json["screens"][index]["data"]), index
    asked, _ = build_form_flow(_at_schema(_BOUNDED), pages)
    assert not set(_AT_CARRIERS) & set(asked["screens"][1]["data"])


def test_an_asks_later_screen_date_time_field_has_no_bound_carriers_carried_to_its_screen():
    pages = [{"title": "One", "fields": ["label"]}, {"title": "Two", "fields": ["at"]}]
    flow_json, _ = build_form_flow(_at_schema(_BOUNDED), pages)
    for index in range(2):
        assert not set(_AT_CARRIERS) & set(flow_json["screens"][index]["data"]), index
    assert not set(_AT_CARRIERS) & set(_footer_payload(flow_json, 0))


def test_the_schema_without_bound_values_strips_a_date_time_fields_bounds():
    from tai42_channel_whatsapp.flows_dates import schema_without_date_bound_values

    assert schema_without_date_bound_values(_at_schema(_BOUNDED)) == _at_schema({})


# --- a date field drawn as a choice control ------------------------------------------------------

_DATE_CHOICES = {"enum": ["2026-01-05", "2026-01-06"], "minDate": "2026-01-01"}


def test_a_date_field_with_an_enum_draws_a_choice_control_with_no_bound_carriers():
    flow_json, _ = build_form_flow(_schema(_DATE_CHOICES))
    control = _control(flow_json, "when")
    assert control["type"] != "CalendarPicker"
    assert not set(_BOUND_MEMBERS) & set(control)
    assert not set(_BOUND_CARRIERS) & set(flow_json["screens"][0]["data"])
    assert not set(_BOUND_CARRIERS) & set(build_flow_data(_schema(_DATE_CHOICES), {}, {}))


def test_a_date_field_with_per_send_options_draws_a_dropdown_with_no_bound_carriers():
    schema = _schema({"minDate": "2026-01-01"})
    pages = [{"title": "One", "fields": ["label"]}, {"title": "Two", "fields": ["when"]}]
    flow_json, _ = build_form_flow(schema, pages, option_fields={"when"})
    assert _control(flow_json, "when", 1)["type"] == "Dropdown"
    for index in range(2):
        assert not set(_BOUND_CARRIERS) & set(flow_json["screens"][index]["data"]), index
    assert not set(_BOUND_CARRIERS) & set(_footer_payload(flow_json, 0))
    options = {"when": [{"value": "2026-01-05", "label": "5 Jan"}]}
    assert not set(_BOUND_CARRIERS) & set(build_flow_data(schema, {}, options, pages))


def test_a_range_pair_whose_start_is_a_choice_draws_two_controls():
    schema = _pair_schema({"enum": ["2026-04-01", "2026-04-02"]}, {"minDate": "2026-04-01"})
    flow_json, _ = build_form_flow(schema, _PAIR_PAGES)
    assert _control(flow_json, "start", 1)["type"] != "CalendarPicker"
    assert _control(flow_json, "end", 1)["min-date"] == "${data.end__min}"
    data = build_flow_data(schema, {}, {}, _PAIR_PAGES)
    assert data["start__init"] == ""
    assert data["end__min"] == "2026-04-01"
    assert "start__min" not in data


def test_an_ask_less_endpoint_form_carries_a_choice_date_fields_bounds_for_its_data_endpoint():
    flow_json, _ = build_form_flow(_schema(_DATE_CHOICES), askless=True)
    assert flow_json["data_api_version"] == "3.0"
    assert flow_json["screens"][0]["data"]["when__min"] == {"type": "string", "__example__": ""}
    assert build_flow_data(_schema(_DATE_CHOICES), {}, {}, askless=True)["when__min"] == "2026-01-01"


# --- a property named like a bound carrier ----------------------------------------------------------


def test_an_endpoint_driven_form_refuses_a_property_named_like_a_bound_carrier():
    schema = _schema(
        {"minDate": "2026-01-01"}, email={"type": "string", "format": "email"}, when__min={"type": "string"}
    )
    with pytest.raises(ChannelInputError, match="collide"):
        build_form_flow(schema)
