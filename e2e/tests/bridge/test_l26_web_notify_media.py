"""L26 — a web notify with a ``data:image``, stored by reference, end to end.

``notify_user(channel="web", media=[data:image])`` decodes the inline image once, holds
the bytes under a random capability id, and appends ONE ``chat.media`` card to the visitor's
transcript whose media url is the ABSOLUTE served reference
(``<public_base_url>/api/interactions/media/<id>``) — a vendor (or the visitor's browser)
fetches it from that url, never the inline bytes. The served-media door returns the exact
bytes with the right mime. The web channel is driven because its own public doors ARE the
medium — the visitor's SSE stream is the card surface. The stack pins
``INTERACTIONS_PUBLIC_BASE_URL`` to replica B's origin (``replica_b_origin_env_keys``), which
is what the seam mints the absolute url from.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable

import httpx

from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
_DATA_IMAGE = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()
_MEDIA_REF_RE = re.compile(r"/api/interactions/media/[A-Za-z0-9_-]{43}")


def _base_url(bridge: BridgeHarness) -> str:
    return f"http://{bridge.stack.host}:{bridge.stack.port_b}"


async def test_web_notify_data_image_stored_by_reference_and_served(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    web, page = await WebChatClient.open_page(
        _base_url(bridge), uniq("l26-site").replace("_", "-"), store_url=bridge.stack.resources.redis_url
    )
    assert page.status_code == 200, page.text

    message = uniq("l26-shot")
    # The agent-initiated notify carries an inline data:image; the notify seam stores it by
    # reference and swaps the url for an ABSOLUTE served reference before the card is built.
    await bridge.api().post(
        "/api/notifications",
        json={
            "message": message,
            "channel": "web",
            "recipient": web.recipient,
            "media": [{"kind": "image", "url": _DATA_IMAGE, "caption": "chart"}],
        },
    )

    def _is_card(event: str, data: dict) -> bool:
        return event == "chat.media" and data.get("text") == message

    frames = await web.frames(until=_is_card)
    card = next(data for event, data in frames if _is_card(event, data))
    media_item = card["media"][0]
    assert media_item["kind"] == "image"
    assert media_item.get("caption") == "chart"
    # The delivered url is the ABSOLUTE served reference on replica B's public base url —
    # never the inline data: bytes.
    served_url = media_item["url"]
    assert served_url.startswith(_base_url(bridge) + "/api/interactions/media/"), served_url
    assert _MEDIA_REF_RE.search(served_url), served_url

    # The served-media door returns the exact bytes with the right mime (the id IS the
    # secret; no auth needed).
    async with httpx.AsyncClient(timeout=5.0) as client:
        served = await client.get(served_url)
    assert served.status_code == 200, served.text
    assert served.headers["content-type"] == "image/png"
    assert served.content == _PNG_BYTES
