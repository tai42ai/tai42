"""A notification/interaction body model refuses an undeclared top-level key.

The CLI, the HTTP API, the builtin tools and the in-process send doors all validate a
caller's body into these models, so an unknown / mis-nested / typo'd key is refused at
every door at once instead of being dropped.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

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
def test_body_model_refuses_an_undeclared_top_level_key(model: type[BaseModel], kwargs: dict[str, Any]):
    with pytest.raises(ValidationError) as caught:
        model.model_validate({**kwargs, "nope": "surprise"})
    assert [(error["type"], error["loc"]) for error in caught.value.errors()] == [("extra_forbidden", ("nope",))]


@pytest.mark.parametrize(("model", "kwargs"), _VALID_BODY, ids=lambda value: getattr(value, "__name__", ""))
def test_body_model_dump_round_trips(model: type[BaseModel], kwargs: dict[str, Any]):
    # A stored dump reads back: the models carry no computed field, serializer or alias.
    built = model.model_validate(kwargs)
    assert model.model_validate(built.model_dump()) == built
    assert model.model_validate(built.model_dump(mode="json")) == built
