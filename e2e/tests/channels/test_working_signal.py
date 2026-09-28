"""The skeleton's per-turn working-on-it (typing) refresh loop, end to end.

A channel that advertises a vendor working-on-it indicator has it refreshed for the life of a
bridged turn: the skeleton spawns one detached loop at the schedule seam, re-asserts the
indicator before it lapses, and stops the instant the turn's answer is about to send (or at a hard
ceiling). These legs drive that over a real bridged turn on the ``stub_working`` counting channel,
which RPUSHes every ``signal_working`` call onto a harness probe list:

* a long-held turn is refreshed at least twice and stops once the record leaves the in-flight
  states (the answer sent);
* a low ``CONVERSATIONS_WORKING_SIGNAL_MAX_SECONDS`` ceiling caps the count even on a long turn;
* a channel advertising no indicator (``stub_working_off``) is never signalled;
* an api-door turn (no channel) is never signalled.

The stub channel registers no inbound HTTP door, so the ``e2e_channel_inbound`` probe drives the
same ``tai42_app.conversations.accept`` seam a real adapter calls; ``e2e_overlap_probe`` holds the
bridged turn open past several refresh intervals.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

# An https callback with no server behind it — an api route needs a callback URL; the api-door leg
# never asserts delivery, only that no working signal fires, so the callback need not connect.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"

_IN_FLIGHT_DONE = {"provisional", "delivered"}


def _overlap_start_expr(marker: str, *, hold_seconds: float) -> str:
    """The ``start_expr`` mapping a turn onto ``e2e_overlap_probe`` kwargs: the marker key it
    RPUSHes its turn-entered barrier under, the whole turn text, and the hold that keeps the
    turn open across the refresh intervals."""
    return f'{{key: "{marker}", message: .message, hold_seconds: {hold_seconds}}}'


async def _mint_key(stack: TaiStack, user_id: str) -> None:
    await stack.api(stack.port_b).post(
        "/api/auth/api-keys",
        json={"user_id": user_id, "description": "e2e working-signal key", "scopes": ["e2e-all"]},
    )


async def _create_channel_tool_route(
    stack: TaiStack, *, route_name: str, execution_key: str, channel: str, our_identity: str, start_expr: str
) -> None:
    await stack.api(stack.port_b).post(
        f"/api/conversations/{route_name}",
        json={
            "door": "channel",
            "target_kind": "tool",
            "target_name": "e2e_overlap_probe",
            "execution_key": execution_key,
            "channel": channel,
            "our_identity": our_identity,
            "start_expr": {"content": start_expr},
        },
    )


async def _create_api_tool_route(stack: TaiStack, *, route_name: str, execution_key: str, start_expr: str) -> None:
    await stack.api(stack.port_b).post(
        f"/api/conversations/{route_name}",
        json={
            "door": "api",
            "target_kind": "tool",
            "target_name": "e2e_overlap_probe",
            "execution_key": execution_key,
            "callback_url": _UNREACHABLE_CALLBACK,
            "start_expr": {"content": start_expr},
        },
    )


async def _drive_stub_inbound(
    stack: TaiStack,
    root_token: str,
    *,
    channel: str,
    our_identity: str,
    client_address: str,
    provider_message_id: str,
    text: str,
) -> str:
    """Accept one inbound on ``channel`` through the SUT-side probe and return the record id."""
    async with stack.mcp(port=stack.port_a, auth=root_token) as mcp:
        result = await mcp.call_tool(
            "e2e_channel_inbound",
            {
                "channel": channel,
                "our_identity": our_identity,
                "client_address": client_address,
                "provider_message_id": provider_message_id,
                "text": text,
            },
            retry_on_reloading=True,
        )
    assert not result.is_error, result
    return result.data["message_id"]


def _signal_records(stack: TaiStack, provider_message_id: str) -> list[dict]:
    """Every ``signal_working`` call recorded for one turn, oldest first."""
    return [json.loads(raw) for raw in stack.records(f"working_signal:{provider_message_id}")]


def _active_count(records: list[dict]) -> int:
    return sum(1 for record in records if record["active"])


def _all_signal_recipients(stack: TaiStack) -> set[str]:
    """The recipient of every ``signal_working`` call recorded across all turns on the stack."""
    recipients: set[str] = set()
    for key in stack.record_keys():
        if not key.startswith("working_signal:"):
            continue
        recipients.update(json.loads(raw)["recipient"] for raw in stack.records(key))
    return recipients


async def _wait_min_active(stack: TaiStack, provider_message_id: str, count: int, *, deadline: float) -> list[dict]:
    async def probe() -> list[dict] | None:
        records = _signal_records(stack, provider_message_id)
        return records if _active_count(records) >= count else None

    return await wait_for_async(
        probe, deadline=deadline, message=f"fewer than {count} working-signal refreshes on {provider_message_id!r}"
    )


async def _wait_record_status(
    stack: TaiStack, route_name: str, message_id: str, statuses: set[str], *, deadline: float
) -> dict:
    async def probe() -> dict | None:
        record = await stack.api(stack.port_b).get(f"/api/conversations/{route_name}/messages/{message_id}")
        return record if record.get("delivery_status") in statuses else None

    return await wait_for_async(probe, deadline=deadline, message=f"record {message_id} never reached {statuses}")


async def _wait_barrier(stack: TaiStack, marker: str, *, deadline: float) -> None:
    async def probe() -> bool | None:
        return True if stack.records(marker) else None

    await wait_for_async(probe, deadline=deadline, message=f"turn barrier {marker!r} never recorded")


async def test_working_signal_refreshes_over_a_long_turn_then_stops_at_the_first_send(
    working_signal_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, root_token = working_signal_stack
    route_name = uniq("wsig-route").replace("_", "-")
    execution_key = uniq("wsig-exec")
    our_identity = uniq("wsig-id").replace("_", "-")
    client_address = uniq("wsigclient").replace("_", "-")
    provider_message_id = uniq("wsigpmid").replace("_", "-")
    marker = uniq("wsig-barrier")

    await _mint_key(stack, execution_key)
    await _create_channel_tool_route(
        stack,
        route_name=route_name,
        execution_key=execution_key,
        channel="stub_working",
        our_identity=our_identity,
        start_expr=_overlap_start_expr(marker, hold_seconds=4.0),
    )

    message_id = await _drive_stub_inbound(
        stack,
        root_token,
        channel="stub_working",
        our_identity=our_identity,
        client_address=client_address,
        provider_message_id=provider_message_id,
        text=uniq("wsig-text"),
    )

    # The turn is running (the probe barrier landed) and the loop refreshed the indicator at least
    # twice while it held.
    await _wait_barrier(stack, marker, deadline=20.0)
    await _wait_min_active(stack, provider_message_id, 2, deadline=25.0)

    # The turn's answer sent, so the record left the in-flight states.
    await _wait_record_status(stack, route_name, message_id, _IN_FLIGHT_DONE, deadline=30.0)

    # Give the loop's first-send cancel a moment to fire its clear, then assert it stopped: at least
    # two asserts went out, a clear (active=False) followed, and every call named this turn.
    async def _cleared() -> list[dict] | None:
        records = _signal_records(stack, provider_message_id)
        return records if any(not record["active"] for record in records) else None

    records = await wait_for_async(
        _cleared, deadline=15.0, message="the working-signal loop never cleared after the send"
    )
    assert _active_count(records) >= 2, records
    assert records[-1]["active"] is False, records
    assert {record["recipient"] for record in records} == {client_address}, records
    assert {record["provider_message_id"] for record in records} == {provider_message_id}, records


async def test_a_low_ceiling_caps_the_working_signal_count(
    working_signal_capped_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, root_token = working_signal_capped_stack
    route_name = uniq("wsigcap-route").replace("_", "-")
    execution_key = uniq("wsigcap-exec")
    our_identity = uniq("wsigcap-id").replace("_", "-")
    client_address = uniq("wsigcapclient").replace("_", "-")
    provider_message_id = uniq("wsigcappmid").replace("_", "-")
    marker = uniq("wsigcap-barrier")

    await _mint_key(stack, execution_key)
    await _create_channel_tool_route(
        stack,
        route_name=route_name,
        execution_key=execution_key,
        channel="stub_working",
        our_identity=our_identity,
        # Held far longer than the ceiling, so an uncapped loop would refresh ~6 times.
        start_expr=_overlap_start_expr(marker, hold_seconds=6.0),
    )

    message_id = await _drive_stub_inbound(
        stack,
        root_token,
        channel="stub_working",
        our_identity=our_identity,
        client_address=client_address,
        provider_message_id=provider_message_id,
        text=uniq("wsigcap-text"),
    )

    await _wait_barrier(stack, marker, deadline=20.0)
    # The loop asserts at least once before the low ceiling stops it.
    await _wait_min_active(stack, provider_message_id, 1, deadline=15.0)
    # The turn stays held well past the ceiling; once it finally sends, the record leaves the
    # in-flight states.
    await _wait_record_status(stack, route_name, message_id, _IN_FLIGHT_DONE, deadline=30.0)

    records = _signal_records(stack, provider_message_id)
    # The ceiling bounded the refreshes: a 6s turn at a ~1s interval would produce ~6 asserts, but
    # the low ceiling caps it to a couple.
    assert 1 <= _active_count(records) <= 3, records
    # The ceiling break clears the indicator once.
    assert any(not record["active"] for record in records), records


async def test_no_working_signal_when_the_channel_advertises_no_expiry(
    working_signal_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, root_token = working_signal_stack
    route_name = uniq("wsigoff-route").replace("_", "-")
    execution_key = uniq("wsigoff-exec")
    our_identity = uniq("wsigoff-id").replace("_", "-")
    client_address = uniq("wsigoffclient").replace("_", "-")
    provider_message_id = uniq("wsigoffpmid").replace("_", "-")
    marker = uniq("wsigoff-barrier")

    await _mint_key(stack, execution_key)
    await _create_channel_tool_route(
        stack,
        route_name=route_name,
        execution_key=execution_key,
        channel="stub_working_off",
        our_identity=our_identity,
        start_expr=_overlap_start_expr(marker, hold_seconds=2.0),
    )

    message_id = await _drive_stub_inbound(
        stack,
        root_token,
        channel="stub_working_off",
        our_identity=our_identity,
        client_address=client_address,
        provider_message_id=provider_message_id,
        text=uniq("wsigoff-text"),
    )

    # The turn ran and its answer delivered, so the loop had every chance to start — and did not,
    # because the channel advertises no vendor indicator (its ``signal_working`` is never called).
    await _wait_barrier(stack, marker, deadline=20.0)
    await _wait_record_status(stack, route_name, message_id, _IN_FLIGHT_DONE, deadline=30.0)
    assert _signal_records(stack, provider_message_id) == []


async def test_api_door_turn_never_starts_a_working_signal(
    working_signal_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, _root_token = working_signal_stack
    route_name = uniq("wsigapi-route").replace("_", "-")
    execution_key = uniq("wsigapi-exec")
    external_user_id = uniq("wsigapi-user").replace("_", "-")
    marker = uniq("wsigapi-barrier")

    await _mint_key(stack, execution_key)
    await _create_api_tool_route(
        stack,
        route_name=route_name,
        execution_key=execution_key,
        start_expr=_overlap_start_expr(marker, hold_seconds=0.0),
    )

    created = await stack.api(stack.port_b).post(
        "/api/auth/api-keys",
        json={"user_id": uniq("wsigapi-caller"), "description": "e2e caller", "scopes": ["e2e-all"]},
    )
    caller = stack.api(stack.port_b).with_token(created["api_key"])

    # The api door builds its record with no channel, so the loop's ``record.channel is None`` gate
    # returns before it starts — the turn runs and answers with no working signal. An api-door
    # record carries no provider message id, so a signal for it would land under the ``None`` key,
    # and its composed address embeds the external user id.
    data = await caller.post(
        f"/api/conversations/{route_name}/messages",
        json={"external_user_id": external_user_id, "text": uniq("wsigapi-text"), "wait_seconds": 20},
    )
    assert data["answer"]["status"] == "answered", data
    assert _signal_records(stack, "None") == []
    assert not any(external_user_id in recipient for recipient in _all_signal_recipients(stack))
