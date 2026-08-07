"""The recycle bus op + the single-shot post-terminal-reply slot.

The recycle handler ARMS the bus's post-reply slot and returns its applied payload;
the subscription loop fires the slot AFTER the terminal reply ships and only on a
clean apply. Scheduling the exit inside the handler would race the reply, so the
handler never schedules — it only arms.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.app.bus import OriginKind, WorkerBus, make_origin
from tai42_skeleton.app.bus_settings import BusRedisSettings, BusSettings
from tai42_skeleton.app.graceful_exit import request_backend_graceful_exit, request_serve_graceful_exit
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin

tai42_app.bind(app)


def _make_bus(kind: OriginKind = OriginKind.serve) -> WorkerBus:
    settings = BusSettings(redis=BusRedisSettings(redis_url="redis://fake"))
    return WorkerBus(settings, make_origin(kind))


class _RecordingRedis:
    """Records the ordered stream of reply publishes so the slot's fire order relative
    to the terminal reply is directly observable."""

    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events

    async def publish(self, channel: str, data: str) -> None:
        self.events.append(("reply", json.loads(data)["phase"]))

    async def exists(self, _key: str) -> int:  # pragma: no cover - unused by these paths
        return 1


def _frame(reply_to: str | None) -> dict[str, Any]:
    frame: dict[str, Any] = {"op": "recycle", "origin": "recycle-origin-other"}
    if reply_to is not None:
        frame["reply_to"] = reply_to
    return {"data": json.dumps(frame)}


# -- slot fires AFTER the terminal reply, only on a clean apply ----------------


async def test_slot_fires_after_the_terminal_reply() -> None:
    bus = _make_bus()
    events: list[tuple[str, Any]] = []
    r = _RecordingRedis(events)

    async def handler(_op: dict[str, Any]) -> dict[str, str]:
        bus.arm_post_reply(lambda: events.append(("slot", None)))
        return {"recycling": "serve"}

    await bus._handle_op(cast("Any", r), bus.origin, handler, _frame("reply-chan"))

    # received ack, THEN the terminal reply, THEN the armed self-exit — never before.
    assert events == [("reply", "received"), ("reply", "terminal"), ("slot", None)]


async def test_slot_fires_even_without_a_reply_channel() -> None:
    # Exit is conditional on apply-success, not on a reply being requested.
    bus = _make_bus()
    events: list[tuple[str, Any]] = []
    r = _RecordingRedis(events)

    async def handler(_op: dict[str, Any]) -> None:
        bus.arm_post_reply(lambda: events.append(("slot", None)))
        return None

    await bus._handle_op(cast("Any", r), bus.origin, handler, _frame(None))
    assert events == [("slot", None)]


async def test_a_failed_apply_disarms_without_firing() -> None:
    bus = _make_bus()
    events: list[tuple[str, Any]] = []
    r = _RecordingRedis(events)

    async def handler(_op: dict[str, Any]) -> None:
        bus.arm_post_reply(lambda: events.append(("slot", None)))
        raise RuntimeError("apply blew up")

    await bus._handle_op(cast("Any", r), bus.origin, handler, _frame("reply-chan"))

    # The terminal reply is `failed`, and the slot is consumed WITHOUT firing.
    assert events == [("reply", "received"), ("reply", "terminal")]
    assert bus._post_reply_slot is None


async def test_arming_then_a_clean_op_that_does_not_rearm_does_not_refire() -> None:
    # The slot is single-shot: a second op that never rearms leaves nothing to fire.
    bus = _make_bus()
    events: list[tuple[str, Any]] = []
    r = _RecordingRedis(events)

    async def arming(_op: dict[str, Any]) -> None:
        bus.arm_post_reply(lambda: events.append(("slot", None)))

    async def plain(_op: dict[str, Any]) -> None:
        return None

    await bus._handle_op(cast("Any", r), bus.origin, arming, _frame("reply-chan"))
    await bus._handle_op(cast("Any", r), bus.origin, plain, _frame("reply-chan"))
    assert events.count(("slot", None)) == 1


async def test_terminal_reply_publish_error_disarms_without_firing() -> None:
    # A transport error shipping the terminal reply propagates AND leaves the slot
    # disarmed, so a later clean op never fires the stale self-exit.
    bus = _make_bus()
    events: list[tuple[str, Any]] = []
    fired: list[str] = []

    class _RaisingOnTerminal:
        async def publish(self, _channel: str, data: str) -> None:
            phase = json.loads(data)["phase"]
            events.append(("reply", phase))
            if phase == "terminal":
                raise ConnectionError("bus publish failed")

    async def arming(_op: dict[str, Any]) -> None:
        bus.arm_post_reply(lambda: fired.append("slot"))

    r = _RaisingOnTerminal()
    with pytest.raises(ConnectionError):
        await bus._handle_op(cast("Any", r), bus.origin, arming, _frame("reply-chan"))

    # The received ack shipped, the terminal publish raised, and the slot is disarmed
    # WITHOUT firing.
    assert events == [("reply", "received"), ("reply", "terminal")]
    assert fired == []
    assert bus._post_reply_slot is None

    # A subsequent clean op that never rearms must not misfire the stale self-exit.
    async def plain(_op: dict[str, Any]) -> None:
        return None

    await bus._handle_op(cast("Any", r), bus.origin, plain, _frame(None))
    assert fired == []


async def test_cancellation_after_arming_disarms_the_slot() -> None:
    # A cancelled callback re-raises, but the slot must still be disarmed on the way out.
    bus = _make_bus()
    fired: list[str] = []
    r = _RecordingRedis([])

    async def arming_then_cancelled(_op: dict[str, Any]) -> None:
        bus.arm_post_reply(lambda: fired.append("slot"))
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await bus._handle_op(cast("Any", r), bus.origin, arming_then_cancelled, _frame("reply-chan"))

    assert fired == []
    assert bus._post_reply_slot is None


# -- the recycle op dispatches to the origin's graceful-exit primitive ---------


class _Mixin(TaiMCPLifecycleMixin):
    """Concrete-enough mixin for the dispatch logic: no server, no config manager."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preset_manager = cast("Any", SimpleNamespace(reconcile_bases=AsyncMock()))

    def _mcp_tools(self, config: Any, tools: Any) -> None:  # pragma: no cover - unused here
        self._mcp_bound_tools[config.title] = {f"{config.title}_t"}


@pytest.mark.parametrize(
    ("kind", "primitive"),
    [
        (OriginKind.serve, request_serve_graceful_exit),
        (OriginKind.backend, request_backend_graceful_exit),
    ],
)
async def test_recycle_dispatch_arms_the_kind_primitive(kind: OriginKind, primitive: Any) -> None:
    m = _Mixin()
    m._bus = _make_bus(kind)

    result = await m._dispatch_bus_op({"op": "recycle"})

    # The applied payload names the graceful-exit kind only — never an env value.
    assert result == {"recycling": kind.value}
    # The slot is armed with this process's graceful self-exit; it fires only later,
    # after the terminal reply ships (proven by the bus tests above).
    assert m._bus._post_reply_slot is primitive


async def test_apply_bus_op_recycle_returns_payload_and_fires_hook() -> None:
    m = _Mixin()
    m._bus = _make_bus(OriginKind.backend)
    fired: list[str] = []
    m._on_fleet_op_applied(lambda name: fired.append(name))

    result = await m._apply_bus_op({"op": "recycle"})

    assert result == {"recycling": "backend"}
    assert fired == ["recycle"]
