"""The one rich-send capability table both channel gates read, and the per-part channel check."""

from __future__ import annotations

from typing import Any, cast

import pytest
from tai42_contract.channels import Channel, ChannelTemplate

from tai42_skeleton.channels.capabilities import RICH_CAPABILITIES, check_channel_part, unsupported_capability


class _TextOnly:
    """A neutral channel that advertises no rich capability (it declares no flag at all)."""


class _Interactive:
    supports_interactive_notifications = True


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("media", ("media", "media")),
        ("location", ("location", "location")),
        ("template", ("template", "template")),
        ("options", ("interactive options", "interactive")),
        ("sections", ("interactive sections", "interactive")),
        ("schema", ("form", "form")),
    ],
)
def test_a_set_field_a_channel_lacks_names_its_label_and_word(field: str, expected: tuple[str, str]) -> None:
    assert unsupported_capability(_TextOnly(), {field: object()}) == expected  # type: ignore[arg-type]


def test_unset_and_ungated_fields_pass() -> None:
    values: dict[str, Any] = {field: None for field, *_ in RICH_CAPABILITIES}
    values.update(header="h", footer="f", data={"k": 1}, pages=[1])
    assert unsupported_capability(_TextOnly(), values) is None  # type: ignore[arg-type]


def test_an_advertised_flag_admits_its_fields_and_the_first_miss_wins() -> None:
    channel = _Interactive()
    assert unsupported_capability(channel, {"options": [1], "sections": [1]}) is None  # type: ignore[arg-type]
    assert unsupported_capability(channel, {"options": [1], "schema": {}, "media": [1]}) == (  # type: ignore[arg-type]
        "media",
        "media",
    )


# -- the per-part channel check -------------------------------------------------


class _CheckingChannel:
    """Records every call to its template and form hooks; refuses a template carrying ``reject``."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def validate_template(self, template: ChannelTemplate) -> None:
        self.calls.append(("template", template))
        if template.parameters and "reject" in template.parameters:
            raise ValueError("template parameter 'reject' has no slot")

    def validate_form_schema(self, schema: dict[str, Any], question: str) -> None:
        self.calls.append(("form", schema, question))


_FORM = {"type": "object", "properties": {"city": {"type": "string"}}}


def test_check_channel_part_calls_the_template_hook_for_a_template():
    channel = _CheckingChannel()
    template = ChannelTemplate(name="status_update", language="en_US", parameters={"ref": "A-42"})

    check_channel_part(cast("Channel", channel), message="ready", template=template, schema=None)

    assert channel.calls == [("template", template)]


def test_check_channel_part_runs_the_form_check_with_the_given_message():
    channel = _CheckingChannel()

    check_channel_part(cast("Channel", channel), message="which city?", template=None, schema=_FORM)

    assert channel.calls == [("form", _FORM, "which city?")]


def test_check_channel_part_refuses_a_form_outside_the_subset():
    channel = _CheckingChannel()

    with pytest.raises(ValueError, match="'address'"):
        check_channel_part(
            cast("Channel", channel),
            message="where?",
            template=None,
            schema={"type": "object", "properties": {"address": {"type": "object"}}},
        )
    assert channel.calls == []


def test_check_channel_part_checks_nothing_for_a_part_with_neither():
    channel = _CheckingChannel()

    check_channel_part(cast("Channel", channel), message="hello", template=None, schema=None)

    assert channel.calls == []


def test_check_channel_part_passes_a_template_through_a_channel_without_the_hook():
    template = ChannelTemplate(name="status_update", language="en_US", parameters={"reject": True})

    check_channel_part(cast("Channel", _TextOnly()), message="ready", template=template, schema=None)


def test_check_channel_part_raises_the_hook_refusal_unchanged():
    channel = _CheckingChannel()
    template = ChannelTemplate(name="status_update", language="en_US", parameters={"reject": True})

    with pytest.raises(ValueError, match=r"^template parameter 'reject' has no slot$"):
        check_channel_part(cast("Channel", channel), message="ready", template=template, schema=None)
