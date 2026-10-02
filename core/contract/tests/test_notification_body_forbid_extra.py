"""Every notification/interaction body model forbids an undeclared top-level key.

The models the CLI, the HTTP API and the in-process send doors all validate a caller's
JSON body into set ``extra="forbid"``, so an unknown / mis-nested / typo'd top-level key
raises ``ValidationError`` at the model — the single chokepoint every door flows through —
instead of being silently dropped. These tests assert that at the contract level, so the
guarantee holds for every door at once, not one seam.
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
def test_body_model_rejects_an_undeclared_top_level_key(model: type[BaseModel], kwargs: dict[str, Any]):
    # An undeclared top-level key raises at the model, so every door that validates this body
    # (CLI, HTTP API, operator-send, notify_user) refuses it loudly instead of dropping it.
    with pytest.raises(ValidationError):
        model.model_validate({**kwargs, "nope": "surprise"})
    with pytest.raises(ValidationError):
        model(**kwargs, nope="surprise")  # type: ignore[call-arg]


def test_channel_template_parameters_stay_opaque_under_forbid_extra():
    # ``extra="forbid"`` bounds only the TOP-LEVEL fields of the model; ``parameters`` is a
    # ``dict[str, Any]`` the platform threads unopened, so arbitrary nested keys round-trip.
    template = ChannelTemplate.model_validate(
        {
            "name": "status_update",
            "language": "en_US",
            "parameters": {"header_media": {"kind": "image"}, "buttons": [{"anything": 1}]},
        }
    )
    assert template.parameters == {"header_media": {"kind": "image"}, "buttons": [{"anything": 1}]}


def test_form_data_values_stay_opaque_under_forbid_extra():
    # ``values`` is keyed by the form's own property names — arbitrary keys inside it are the
    # per-send prefill payload, not extra fields on the model; only top-level model keys are forbidden.
    data = FormData.model_validate({"values": {"any_property": 1, "nested": {"k": "v"}}})
    assert data.values == {"any_property": 1, "nested": {"k": "v"}}
