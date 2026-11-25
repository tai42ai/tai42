"""Ordered delivery: the parts of one answer reach the participant in order.

WhatsApp confirms delivery by receipt: an accepted send is not yet in the participant's chat,
so the platform holds each next part until the medium reports the previous one delivered. The
whatsapp stub's automatic receipts are withheld for the answer, so the test decides when each
part is delivered:

* posting ``delivered`` for a part releases the next part at once, and nothing goes out before;
* a part whose receipt never comes holds the next one for the receipt wait
  (``CONVERSATIONS_DELIVERY_RECEIPT_WAIT_SECONDS``, its default here), then the next part goes out
  and the platform logs one WARNING naming the part that was never confirmed.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable

import pytest

from tai42_e2e import wait_absent_async
from tai42_e2e.manifests import BRIDGE_WHATSAPP_CLIENT, BRIDGE_WHATSAPP_PHONE_ID, BRIDGE_WHATSAPP_PHONE_ID_B
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import (
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    script_reply,
    wait_channel_send_count,
)

pytestmark = [
    pytest.mark.needs(
        "kind:channels:whatsapp",
        "kind:identity",
        "helper:llm",
        "helper:whatsapp",
        "setting:agent",
        "setting:conversations",
        "setting:seeded-access-control",
    ),
    pytest.mark.skipif(
        HarnessSettings().is_real("whatsapp") or HarnessSettings().is_real("llm"),
        reason="FakeWhatsApp + scripted-LLM is the 'whatsapp'/'llm' mock leg; real legs under TAI_E2E_REAL",
    ),
]

# The deployment's receipt wait: the stack leaves CONVERSATIONS_DELIVERY_RECEIPT_WAIT_SECONDS at
# its default.
_RECEIPT_WAIT_SECONDS = 3.0
# How long a part is watched for a premature next part before its receipt is posted; well inside
# the receipt wait, so the next part can only be released by that receipt.
_HELD_WATCH_SECONDS = 1.0
# Slack for scheduling and the platform's receipt polling when timing a release.
_TIMING_SLACK_SECONDS = 0.5
# Enough tokens to split into three whatsapp messages (max_message_chars['whatsapp'] == 4096),
# each token carrying the marker so every part matches it.
_ANSWER_TOKENS = 450
_PARTS = 3
_WARNING = "reported no delivery receipt within"


def _answer(marker: str) -> str:
    return " ".join(_token(marker, index) for index in range(_ANSWER_TOKENS))


def _token(marker: str, index: int) -> str:
    return f"{marker}-{index:04d}"


def _token_indexes(body: str, marker: str) -> list[int]:
    return [int(word.rsplit("-", 1)[1]) for word in body.split() if word.startswith(f"{marker}-")]


def _log_sizes(stack: TaiStack) -> dict[str, int]:
    return {name: len(_log(stack, name)) for name in stack.procs}


def _log(stack: TaiStack, name: str) -> str:
    return stack.process(name).log_path.read_text(encoding="utf-8", errors="replace")


def _warning_lines(stack: TaiStack, offsets: dict[str, int]) -> list[str]:
    lines: list[str] = []
    for name, offset in offsets.items():
        lines.extend(line for line in _log(stack, name)[offset:].splitlines() if _WARNING in line)
    return lines


async def _ask_for_a_long_answer(bridge: BridgeHarness, uniq: Callable[[str], str], identity: str) -> str:
    """Route ``identity`` to the agent, script a three-part answer and send the question; answer
    the marker every part carries, its automatic receipts withheld."""
    exec_key = uniq("do-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq("do-route").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_name, agent="tools_agent", execution_key=exec_key, channel="whatsapp", our_identity=identity
    )
    marker = uniq("do-ans")
    bridge.fake_whatsapp.receipts.withhold(marker)
    script_reply(bridge.llm_stub, _answer(marker))
    inbound = bridge.whatsapp_inbound(phone_number_id=identity, wa_id=BRIDGE_WHATSAPP_CLIENT, text="tell me a lot")
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, inbound)).status_code == 200
    return marker


def _assert_whole_answer_in_order(parts: list[dict], marker: str) -> None:
    indexes = [index for part in parts for index in _token_indexes(part["body"], marker)]
    assert indexes == list(range(_ANSWER_TOKENS)), "the parts did not carry the answer once, in order"


async def test_each_part_waits_for_the_previous_parts_receipt(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = BRIDGE_WHATSAPP_PHONE_ID
    marker = await _ask_for_a_long_answer(bridge, uniq, identity)

    for count in range(1, _PARTS):
        parts = await wait_channel_send_count(bridge.fake_whatsapp, marker, count)
        held = parts[-1]

        async def premature(sent: int = count) -> list[dict] | None:
            return bridge.fake_whatsapp.sends_matching(marker)[sent:] or None

        await wait_absent_async(
            premature,
            window=max(0.0, held["received_at"] + _HELD_WATCH_SECONDS - time.time()),
            interval=0.1,
            message=f"part {count + 1} went out before part {count} was reported delivered",
        )

        status = bridge.whatsapp_status(
            phone_number_id=identity, wamid=held["wamid"], status="delivered", recipient_id=BRIDGE_WHATSAPP_CLIENT
        )
        assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, status)).status_code == 200

        released = (await wait_channel_send_count(bridge.fake_whatsapp, marker, count + 1, deadline=10.0))[-1]
        assert released["received_at"] - held["received_at"] < _RECEIPT_WAIT_SECONDS - _TIMING_SLACK_SECONDS, (
            f"part {count + 1} went out on the receipt wait, not on part {count}'s receipt"
        )

    _assert_whole_answer_in_order(bridge.fake_whatsapp.sends_matching(marker), marker)


async def test_a_missing_receipt_holds_the_next_part_for_the_wait_then_warns(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    offsets = _log_sizes(bridge.stack)
    marker = await _ask_for_a_long_answer(bridge, uniq, BRIDGE_WHATSAPP_PHONE_ID_B)

    parts = await wait_channel_send_count(bridge.fake_whatsapp, marker, _PARTS, deadline=30.0 + _PARTS * 5.0)

    for previous, following in itertools.pairwise(parts):
        assert following["received_at"] - previous["received_at"] >= _RECEIPT_WAIT_SECONDS - _TIMING_SLACK_SECONDS, (
            "a part went out before the receipt wait for the previous part elapsed"
        )
    _assert_whole_answer_in_order(parts, marker)

    unconfirmed = [part["wamid"] for part in parts[:-1]]

    async def warned() -> list[str] | None:
        lines = _warning_lines(bridge.stack, offsets)
        return lines if all(any(wamid in line for line in lines) for wamid in unconfirmed) else None

    lines = await wait_for_async(warned, deadline=10.0, message=f"no receipt WARNING named each of {unconfirmed}")
    assert all("WARNING" in line for line in lines), lines
    assert len(lines) == len(unconfirmed), f"expected one WARNING per unconfirmed part, saw {lines}"
