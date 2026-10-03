"""A notification/interaction body model ignores an undeclared top-level key.

The models the CLI, the HTTP API and the in-process send doors validate a caller's JSON
body into do not set ``extra="forbid"``, so an unknown / mis-nested / typo'd top-level key
is dropped by ``model_validate`` and the declared fields still validate — a notification
body tolerates an unknown key the same way every other request body does. These tests
assert that at the contract level, so the guarantee holds for every door at once.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from tai42_contract.channels import ChannelTemplate, LinkOption, OptionSection, ReplyOption
from tai42_contract.interactions.models import (
    DisplayBlock,
    FormData,
    FormOption,
    FormPage,
    LocationElement,
    MediaItem,
)

# One valid minimal kwargs set per body model — enough to construct it cleanly, so the only
# thing the extra-key variants below change is the presence of an undeclared top-level key.
_VALID_BODY: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (ChannelTemplate, {"name": "status_update", "language": "en_US"}),
    (LocationElement, {"latitude": 51.5, "longitude": -0.12}),
    (MediaItem, {"kind": "image", "url": "https://cdn.example/a.jpg"}),
    (FormData, {"values": {"name": "Ada"}}),
    (FormOption, {"value": "gold"}),
    (DisplayBlock, {"kind": "body", "text": "hello"}),
    (FormPage, {"title": "You", "fields": ["name"]}),
    (ReplyOption, {"text": "Yes"}),
    (LinkOption, {"label": "Docs", "url": "https://cdn.example/d"}),
    (OptionSection, {"title": "Fruit", "rows": [{"kind": "reply", "text": "Apple"}]}),
]


@pytest.mark.parametrize(("model", "kwargs"), _VALID_BODY, ids=lambda value: getattr(value, "__name__", ""))
def test_body_model_constructs_without_the_extra_key(model: type[BaseModel], kwargs: dict[str, Any]):
    # The baseline the extra-key cases diverge from: the declared shape validates.
    assert model.model_validate(kwargs) is not None


@pytest.mark.parametrize(("model", "kwargs"), _VALID_BODY, ids=lambda value: getattr(value, "__name__", ""))
def test_body_model_ignores_an_undeclared_top_level_key(model: type[BaseModel], kwargs: dict[str, Any]):
    # An undeclared top-level key is dropped, not refused: the model validates and carries only
    # its declared fields, so every door that validates this body accepts an unknown key the same
    # way every other request body does.
    accepted = model.model_validate({**kwargs, "nope": "surprise"})
    assert accepted.model_validate(kwargs) is not None
    assert not hasattr(accepted, "nope")
    assert accepted == model.model_validate(kwargs)
