"""Asserting that a media-bridge channel rejected one inbound: the notice the user reads + the event.

A rejected inbound is acked 2xx (so the vendor stops redelivering), sends the user ONE notice on the
vendor stub, and emits ONE ``conversations_inbound_rejected`` event carrying the reason, observed
through a hook fired on that topic and scoped to the leg's recipient.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from tai42_e2e.provider_stub import SignedInbound
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import BridgeHarness, post_inbound

# The one fixed notice the user reads per rejection reason (mirrors the skeleton chokepoint's copy).
REJECTION_NOTICES = {
    "too_large": "That file is too large to accept here.",
    "unsupported_type": "This content type is not supported here.",
    "could_not_receive": "That attachment could not be received.",
    "unreadable": "That message could not be read. Please send it again.",
}
_REJECTED_TOPIC = "conversations_inbound_rejected"


async def _register_reject_hook(
    bridge: BridgeHarness, uniq: Callable[[str], str], *, probe: str, recipient: str
) -> None:
    """Register a hook recording ``reason`` for a ``conversations_inbound_rejected`` event whose
    ``client_address`` is this test's recipient — so a concurrent rejection never lands in the probe."""
    exec_key = uniq("mrej-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.api().post(
        "/api/hooks",
        json={
            "name": uniq("mrej-hook").replace("_", "-"),
            "topic": _REJECTED_TOPIC,
            "tool": "e2e_record",
            "start_expr": {
                "content": f'if .client_address == "{recipient}" then {{key: "{probe}", value: .reason}} else null end'
            },
            "execution_key": exec_key,
        },
    )


def notices_to(fake: Any, notice: str, recipient: str, recipient_field: str) -> list[dict]:
    """Recorded sends carrying the ``notice`` copy addressed to ``recipient`` (scoped so a stale
    send from another test on the session-shared stub is never counted)."""
    return [record for record in fake.sends_matching(notice) if record.get(recipient_field) == recipient]


async def assert_rejected(
    bridge: BridgeHarness,
    uniq: Callable[[str], str],
    *,
    inbound_path: str,
    inbound: SignedInbound,
    fake: Any,
    recipient: str,
    recipient_field: str,
    reason: str,
    notice: str | None = None,
) -> None:
    """Post an inbound the channel rejects; assert the 2xx ack + one reason notice + one event.

    ``notice`` is the copy the user reads: the route's own sentence when the leg's route words it,
    else the built-in notice for ``reason``."""
    expected_notice = REJECTION_NOTICES[reason] if notice is None else notice
    probe = uniq("mrej-probe")
    await _register_reject_hook(bridge, uniq, probe=probe, recipient=recipient)

    resp = await post_inbound(bridge.stack, inbound_path, inbound, port=bridge.stack.port_b)
    assert resp.status_code in (200, 204), resp.text

    async def _notice_landed() -> list[dict] | None:
        got = notices_to(fake, expected_notice, recipient, recipient_field)
        return got if got else None

    notices = await wait_for_async(_notice_landed, deadline=12.0, message=f"the {reason} notice never reached the stub")
    assert len(notices) == 1, f"expected exactly one {reason} notice, saw {notices!r}"

    async def _event_recorded() -> list[str] | None:
        rows = [json.loads(raw)["value"] for raw in bridge.stack.records(probe)]
        return rows if rows else None

    reasons = await wait_for_async(_event_recorded, deadline=12.0, message="the rejection event never fired the hook")
    assert reasons == [reason], f"expected one {reason} event, saw {reasons!r}"
