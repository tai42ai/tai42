"""A tap on a choice the channel can no longer read gets the one choice-unavailable notice + event.

The user reads "That choice is no longer available. Please type your reply as a message." once, the
platform emits one ``conversations_inbound_rejected`` event carrying ``reason == "choice_unavailable"``,
and the webhook acks 2xx so the vendor stops redelivering: a telegram button tap whose token no live
question holds and whose message carries no keyboard to read the tapped label from.

Driven over the live media-bridge stack; the channels mock over their in-process stubs, so any real
selection breaks the stubs and the module steps aside.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.settings import HarnessSettings

from ._bridge_support import TELEGRAM_INBOUND_PATH, BridgeHarness
from ._rejection_support import assert_rejected

pytestmark = [
    pytest.mark.needs(
        "kind:identity", "probe-tools", "setting:conversations", "setting:seeded-access-control", "store:redis"
    ),
    pytest.mark.skipif(
        any(HarnessSettings().is_real(seam) for seam in ("telegram", "llm")),
        reason="the media-bridge stubs are the mock leg; real legs run under TAI_E2E_REAL",
    ),
]


@pytest.mark.needs("kind:channels:telegram", "helper:telegram")
async def test_telegram_tap_without_a_readable_label_is_rejected_choice_unavailable(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    chat_id = "970001"
    tap = media_bridge.telegram_callback_query(chat_id=chat_id, message_id=97001, data=uniq("tg-unheld"))
    await assert_rejected(
        media_bridge,
        uniq,
        inbound_path=TELEGRAM_INBOUND_PATH,
        inbound=tap,
        fake=media_bridge.fake_telegram,
        recipient=chat_id,
        recipient_field="chat_id",
        reason="choice_unavailable",
    )
