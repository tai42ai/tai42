"""The size budget of a WhatsApp Flow form's screen data, checked before the data is sent.

Two figures bound the data. An endpoint-driven form's every footer echoes the screen's whole data
to the data endpoint, so its data is no larger than the endpoint can read back
(:func:`~tai42_channel_whatsapp.flows_endpoint.echoed_screen_data_max_bytes`). Slotted pictures
ride the data, so the data of a form with picture slots is also within Meta's figure for data that
carries pictures. A static form without picture slots has no budget here.
"""

from __future__ import annotations

import json
from typing import Any

from tai42_contract.channels import ChannelInputError

from tai42_channel_whatsapp.flows_endpoint import echoed_screen_data_max_bytes
from tai42_channel_whatsapp.flows_resolve import image_slots

# Meta's Flow Image reference: "Total data channel payload size: 1 Mb". Read as the whole
# data of a send or an answer that carries slotted pictures; the figure is documented, and
# what the vendor does above it is not.
_PICTURED_DATA_MAX_BYTES = 1_000_000


def require_screen_data_fits(pages: list[dict[str, Any]] | None, data: dict[str, Any], *, endpoint: bool) -> None:
    """Refuse screen ``data`` too large for its form: endpoint-driven (``endpoint``) or with picture slots.

    The size is the compact JSON's UTF-8 length. The budget is the data the endpoint reads back for
    an endpoint-driven form, Meta's figure for data that carries pictures for a form with picture
    slots, and the smaller of the two for a form that is both. A static form without picture slots
    is not measured. Raises ``ChannelInputError`` over the budget.
    """
    pictured = bool(image_slots(pages))
    if not (pictured or endpoint):
        return
    budgets = ([_PICTURED_DATA_MAX_BYTES] if pictured else []) + ([echoed_screen_data_max_bytes()] if endpoint else [])
    budget = min(budgets)
    size = len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size <= budget:
        return
    if pictured:
        raise ChannelInputError(
            f"form data is {size} bytes, over the {budget} bytes this channel sends in a form with picture "
            "slots; slotted pictures ride this data and count toward it"
        )
    raise ChannelInputError(
        f"form data is {size} bytes, over the {budget} bytes the data endpoint reads back for an endpoint-driven form"
    )
