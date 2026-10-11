"""The one rich-send capability table both channel gates read."""

from __future__ import annotations

from typing import Any

import pytest

from tai42_skeleton.channels.capabilities import RICH_CAPABILITIES, unsupported_capability


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
