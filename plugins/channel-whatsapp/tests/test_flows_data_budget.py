"""The size budget of a WhatsApp Flow form's screen data."""

from __future__ import annotations

import json
from typing import Any

import pytest
from tai42_contract.channels import ChannelInputError

from tai42_channel_whatsapp.flows_data_budget import require_screen_data_fits
from tai42_channel_whatsapp.flows_endpoint import echoed_screen_data_max_bytes


def _page(*display: dict[str, Any]) -> dict[str, Any]:
    return {"title": "Form", "fields": ["note"], "kind": "input", "display": list(display)}


def _data_of_size(size: int) -> dict[str, str]:
    data = {"slot_preview": ""}
    data["slot_preview"] = "A" * (size - len(json.dumps(data, separators=(",", ":"))))
    return data


_PICTURE_SLOT_PAGES = [_page({"kind": "image", "slot": "preview"})]
_PAGES_WITHOUT_PICTURE_SLOTS = [
    None,
    [_page({"kind": "body", "slot": "caption", "text": "Hi"})],
    [_page({"kind": "image", "src": "c3RhdGlj"})],
]
_WITHOUT_PICTURE_SLOT_IDS = ["no-pages", "text-slot-only", "unslotted-image-only"]


@pytest.mark.parametrize(("endpoint", "budget"), [(False, 1_000_000), (True, echoed_screen_data_max_bytes())])
def test_picture_slot_data_at_the_budget_fits_and_one_byte_more_is_refused(endpoint: bool, budget: int):
    require_screen_data_fits(_PICTURE_SLOT_PAGES, _data_of_size(budget), endpoint=endpoint)
    with pytest.raises(ChannelInputError) as caught:
        require_screen_data_fits(_PICTURE_SLOT_PAGES, _data_of_size(budget + 1), endpoint=endpoint)
    assert str(caught.value) == (
        f"form data is {budget + 1} bytes, over the {budget} bytes this channel sends in a form with "
        "picture slots; slotted pictures ride this data and count toward it"
    )


@pytest.mark.parametrize("pages", _PAGES_WITHOUT_PICTURE_SLOTS, ids=_WITHOUT_PICTURE_SLOT_IDS)
def test_endpoint_driven_data_without_picture_slots_at_the_echo_budget_fits_and_one_byte_more_is_refused(
    pages: list[dict[str, Any]] | None,
):
    budget = echoed_screen_data_max_bytes()
    require_screen_data_fits(pages, _data_of_size(budget), endpoint=True)
    with pytest.raises(ChannelInputError) as caught:
        require_screen_data_fits(pages, _data_of_size(budget + 1), endpoint=True)
    assert str(caught.value) == (
        f"form data is {budget + 1} bytes, over the {budget} bytes the data endpoint reads back for an "
        "endpoint-driven form"
    )


@pytest.mark.parametrize("pages", _PAGES_WITHOUT_PICTURE_SLOTS, ids=_WITHOUT_PICTURE_SLOT_IDS)
def test_data_of_a_static_form_without_picture_slots_is_not_measured(pages: list[dict[str, Any]] | None):
    require_screen_data_fits(pages, _data_of_size(2_000_000), endpoint=False)
