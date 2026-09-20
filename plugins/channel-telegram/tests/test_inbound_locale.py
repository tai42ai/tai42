"""The telegram inbound door maps the sender's ``language_code`` to the turn's locale."""

from __future__ import annotations

import pytest

from tai42_channel_telegram.inbound import _inbound_locale


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"message": {"from": {"language_code": "he"}}}, "he"),
        ({"message": {"from": {"language_code": "pt-br"}}}, "pt-BR"),
        ({"callback_query": {"from": {"language_code": "en"}}}, "en"),
    ],
)
def test_maps_language_code(update: dict, expected: str) -> None:
    assert _inbound_locale(update) == expected


@pytest.mark.parametrize(
    "update",
    [
        {"message": {"from": {}}},
        {"message": {"from": {"language_code": ""}}},
        {"message": {"from": {"language_code": "not a locale!"}}},
        {"message": {"text": "hi"}},
        {},
    ],
)
def test_missing_or_malformed_language_drops_to_none(update: dict) -> None:
    assert _inbound_locale(update) is None
