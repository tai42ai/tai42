"""The ``applied`` payloads of the recycle and carry-roll ops are read back by one parser of a named reason."""

from __future__ import annotations

import pytest

from tai42_skeleton.app.bus import ExitRefusal
from tai42_skeleton.app.bus.ops import CarryRollReply, carry_roll_reply, recycle_leaving, recycle_reply


@pytest.mark.parametrize("kind", ["already_leaving", "exit_deferred"])
def test_a_recycle_reply_naming_why_it_armed_nothing_reads_back_as_that_refusal(kind: str) -> None:
    refusal = ExitRefusal(kind=kind, reason="this change")  # type: ignore[arg-type]

    assert recycle_leaving(recycle_reply("serve", refusal)) == refusal


def test_a_recycle_reply_that_armed_the_exit_names_no_refusal() -> None:
    assert recycle_leaving(recycle_reply("serve", None)) is None
    assert recycle_leaving(None) is None


@pytest.mark.parametrize("kind", ["carrying", "already_leaving"])
def test_a_carry_roll_reply_reads_back_as_itself(kind: str) -> None:
    reply = CarryRollReply(kind=kind, reason="this change")  # type: ignore[arg-type]

    assert carry_roll_reply(reply.reply()) == reply


def test_a_payload_naming_no_carry_roll_reply_is_none() -> None:
    assert carry_roll_reply({"status": "ok"}) is None
    assert carry_roll_reply(None) is None


@pytest.mark.parametrize(
    ("parse", "payload"),
    [
        (recycle_leaving, {"recycling": "serve", "already_leaving": 5}),
        (recycle_leaving, {"recycling": "serve", "exit_deferred": ["a change"]}),
        (carry_roll_reply, {"carrying": 5}),
        (carry_roll_reply, {"already_leaving": {"why": "x"}}),
    ],
    ids=["recycle-number", "recycle-list", "carry-number", "carry-object"],
)
def test_a_reply_whose_reason_is_not_text_raises(parse: object, payload: dict[str, object]) -> None:
    with pytest.raises(TypeError, match="names its reason as"):
        parse(payload)  # type: ignore[operator]
