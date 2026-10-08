"""The platform's give-up of an abandoned park: the parking driver tells the waiter it captured.

When the reaper drops a continuation-due record past the redelivery horizon, the platform first
asks the registered give-up handlers to end the park, in the frame a receiver-less drive of the park
runs in, and delivers the outermost outcome a handler returns; with no handler owning the park it
delivers the run's FAILED itself. A neutral test-only driver below keeps its own captured routing per
interaction and fires it through the kit's terminal chain notice to a test-only delivery tool — no
real driver's code takes part.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    AnswerFormat,
    ChainedResume,
    InteractionRequest,
    ResumeBuffered,
    RunFailed,
)
from tai42_contract.states import StateAttach, StateBinding
from tai42_contract.template import TemplatedText
from tai42_contract.tools import get_run_delivery, get_run_delivery_id
from tai42_kit.interactions import park_giveup
from tai42_kit.interactions.park_adoption import terminal_chain_notice
from tai42_kit.interactions.park_giveup import ParkGiveUpOutcome, register_park_giveup_handler

from tai42_skeleton.authz.execution_identity import (
    get_execution_identity,
    reset_execution_identity,
    set_execution_identity,
)
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import giveup_delivery
from tai42_skeleton.interactions import reaper as reaper_module
from tai42_skeleton.runs.chokepoint import get_resume_origin
from tai42_skeleton.tools.state_binding import current_deferred_binding

from ._continuation_support import configure_interactions_store, make_wired

ABANDONED = {"tai42:resume_abandoned": True}


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_interactions_store(monkeypatch)


@pytest.fixture(autouse=True)
def _restore_giveup_handlers() -> Iterator[None]:
    saved = list(park_giveup._park_giveup_handlers)
    park_giveup._park_giveup_handlers.clear()
    yield
    park_giveup._park_giveup_handlers[:] = saved


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, fake_redis: Any, fake_client_ctx: Any) -> SimpleNamespace:
    return make_wired(monkeypatch, fake_redis, fake_client_ctx)


class _Tools:
    """A ``run_tool`` fake that dispatches by name and records each dispatch."""

    def __init__(self, handlers: dict[str, Any]) -> None:
        self.handlers = handlers
        self.calls: list[dict[str, Any]] = []

    async def run_tool(self, key: str, arguments: dict[str, Any], *, offload_sync: bool = False, continues_chain=None):
        self.calls.append({"key": key, "arguments": dict(arguments), "continues_chain": continues_chain})
        handler = self.handlers.get(key)
        return None if handler is None else handler(arguments)


@pytest.fixture
def binds(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Bind a stand-in execution identity for each continuation frame, recording ``(key, fingerprint)``."""
    calls: list[tuple[str, str]] = []

    @asynccontextmanager
    async def _bind(identity: str, *, bound_fingerprint: str = ""):
        calls.append((identity, bound_fingerprint))
        token = set_execution_identity(SimpleNamespace(user_id=identity))  # type: ignore[arg-type]
        try:
            yield
        finally:
            reset_execution_identity(token)

    monkeypatch.setattr(continuation_module, "bind_execution_identity", _bind)
    return calls


def _wire_tools(monkeypatch: pytest.MonkeyPatch, handlers: dict[str, Any]) -> _Tools:
    tools = _Tools(handlers)
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=tools))
    return tools


def _request(interaction_id: str = "iid-g", **overrides: Any) -> InteractionRequest:
    now = datetime.now(UTC)
    fields: dict[str, Any] = {
        "interaction_id": interaction_id,
        "group_id": "gg",
        "question": "?",
        "answer_format": AnswerFormat.TEXT,
        "reply_to": f"reply:{interaction_id}",
        "created_at": now,
        "timeout_at": now + timedelta(hours=1),
        "mode": "async",
        "continuation_tool": "resume_tool",
        "continuation_identity": "svc-key",
        "expiry_at": now + timedelta(hours=1),
        "run_delivery_id": "rd-g",
        "delivery": ("door_address", {"thread_id": "tg"}),
    }
    fields.update(overrides)
    return InteractionRequest(**fields)


def _door_fires(tools: _Tools) -> list[dict[str, Any]]:
    return [call["arguments"] for call in tools.calls if call["key"] == "door_address"]


class _ProbeDriver:
    """A neutral driver: it records the routing it captured for each park and ends a park it owns by firing it."""

    def __init__(self) -> None:
        self.routing: dict[str, ChainedResume] = {}
        self.calls: list[dict[str, Any]] = []

    async def give_up(self, interaction_id: str, failed_outcome: Mapping[str, Any]) -> ParkGiveUpOutcome | None:
        routing = self.routing.get(interaction_id)
        if routing is None:
            return None
        self.calls.append(
            {
                "interaction_id": interaction_id,
                "failed_outcome": dict(failed_outcome),
                "identity": getattr(get_execution_identity(), "user_id", None),
                "origin": get_resume_origin(),
                "run_delivery_id": get_run_delivery_id(),
                "run_delivery": get_run_delivery(),
            }
        )
        tool, payload = terminal_chain_notice(routing, RunFailed(outcome=dict(failed_outcome)))
        return ParkGiveUpOutcome(await tai42_app.tools.run_tool(tool, payload, continues_chain=routing.asked_by))


def _probe(reply: Any) -> tuple[_ProbeDriver, dict[str, Any]]:
    driver = _ProbeDriver()
    driver.routing["iid-g"] = ChainedResume(
        delivery_tool="probe_chain_deliver", chain_key="probe-caller-key", asked_by=("caller",)
    )
    register_park_giveup_handler(driver.give_up)
    fired: dict[str, Any] = {}

    def probe_chain_deliver(arguments: dict[str, Any]) -> Any:
        fired.update(arguments)
        return reply

    return driver, {"probe_chain_deliver": probe_chain_deliver, "fired": fired}


async def test_with_no_owning_handler_the_run_failed_is_delivered(wired, monkeypatch, binds) -> None:
    tools = _wire_tools(monkeypatch, {})
    await giveup_delivery.deliver_park_giveup(wired.store, _request(), fingerprint="fp-g")
    (fire,) = _door_fires(tools)
    assert fire["status"] == PARK_COMPLETION_FAILED
    assert fire["result"] == ABANDONED
    assert fire["completion_id"] == continuation_module._completion_id("rd-g")


async def test_the_owning_driver_tells_its_captured_caller_inside_the_parks_frame(wired, monkeypatch, binds) -> None:
    driver, wiring = _probe(RunFailed(outcome={"caller": "failed too"}))
    tools = _wire_tools(monkeypatch, {"probe_chain_deliver": wiring["probe_chain_deliver"]})

    await giveup_delivery.deliver_park_giveup(wired.store, _request(), fingerprint="fp-g")

    (call,) = driver.calls
    assert call["interaction_id"] == "iid-g"
    assert call["failed_outcome"] == ABANDONED
    assert call["identity"] == "svc-key"
    assert binds == [("svc-key", "fp-g")]
    assert call["origin"] == "iid-g"
    assert call["run_delivery_id"] == "rd-g"
    assert call["run_delivery"] == ("door_address", {"thread_id": "tg"})
    # The stored caller is told, with the routing the driver captured.
    assert wiring["fired"]["chain_token"] == "probe-caller-key"
    assert wiring["fired"]["status"] == PARK_COMPLETION_FAILED
    probe_call = next(c for c in tools.calls if c["key"] == "probe_chain_deliver")
    assert probe_call["continues_chain"] == ("caller",)
    # The handler's RunFailed reaches the run's address as FAILED with its outcome.
    (fire,) = _door_fires(tools)
    assert fire["status"] == PARK_COMPLETION_FAILED
    assert fire["result"] == {"caller": "failed too"}


async def test_a_handled_answer_is_delivered_succeeded_with_the_deferred_binding_applied(
    wired, monkeypatch, binds
) -> None:
    _driver, wiring = _probe("the caller's on_error answer")
    tools = _wire_tools(monkeypatch, {"probe_chain_deliver": wiring["probe_chain_deliver"]})
    applied: list[dict[str, Any]] = []
    seen_binding: list[Any] = []

    async def _apply(**kwargs: Any) -> None:
        applied.append(kwargs)

    real_give_up = _driver.give_up

    async def _give_up(interaction_id: str, failed_outcome: Mapping[str, Any]) -> ParkGiveUpOutcome | None:
        seen_binding.append(current_deferred_binding())
        return await real_give_up(interaction_id, failed_outcome)

    park_giveup._park_giveup_handlers[:] = [_give_up]
    monkeypatch.setattr(continuation_module, "_apply_deferred_binding", _apply)
    binding = StateBinding(states=[StateAttach(state="status", subject_expr=TemplatedText(content=".thread_id"))])
    request = _request(deferred_binding=binding, run_input={"k": "v"}, door_id="door-1")

    await giveup_delivery.deliver_park_giveup(wired.store, request, fingerprint=None)

    assert seen_binding[0] is not None
    assert seen_binding[0].binding == binding
    assert applied == [
        {
            "deferred_binding": binding,
            "run_input": {"k": "v"},
            "door_id": "door-1",
            "output": "the caller's on_error answer",
            "run_delivery_id": "rd-g",
        }
    ]
    (fire,) = _door_fires(tools)
    assert fire["status"] == PARK_COMPLETION_SUCCEEDED
    assert fire["result"] == "the caller's on_error answer"


async def test_a_re_park_returned_by_the_handler_delivers_nothing(wired, monkeypatch, binds) -> None:
    _driver, wiring = _probe(ResumeBuffered(remaining_ids=["sibling"]))
    tools = _wire_tools(monkeypatch, {"probe_chain_deliver": wiring["probe_chain_deliver"]})
    await giveup_delivery.deliver_park_giveup(wired.store, _request(), fingerprint=None)
    assert _door_fires(tools) == []


async def test_a_raising_handler_is_logged_the_failed_still_delivered_and_the_error_re_raised(
    wired, monkeypatch, binds, caplog
) -> None:
    tools = _wire_tools(monkeypatch, {})

    async def broken(interaction_id: str, failed_outcome: Mapping[str, Any]) -> ParkGiveUpOutcome | None:
        raise RuntimeError("lease held")

    register_park_giveup_handler(broken)
    caplog.set_level(logging.ERROR)
    with pytest.raises(RuntimeError, match="lease held"):
        await giveup_delivery.deliver_park_giveup(wired.store, _request(), fingerprint=None)
    assert any("give-up handler" in r.getMessage() for r in caplog.records)
    (fire,) = _door_fires(tools)
    assert fire["status"] == PARK_COMPLETION_FAILED
    assert fire["result"] == ABANDONED


async def test_the_reaper_passes_the_parks_stored_fingerprint_to_the_give_up(wired, monkeypatch) -> None:
    request = _request("iid-r")
    await wired.store.add(
        wired.fake,
        request,
        wired.settings.idle_ttl_seconds,
        continuation_fingerprint="fp-stored",
    )
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    await wired.fake.zadd(wired.store.continuation_due_index_key, {"iid-r": now_ms - 1000})
    monkeypatch.setattr(reaper_module, "InteractionStore", lambda _prefix: wired.store)
    seen: list[tuple[str, str | None]] = []

    async def _giveup(store: Any, req: InteractionRequest, *, fingerprint: str | None) -> None:
        seen.append((req.interaction_id, fingerprint))

    monkeypatch.setattr(reaper_module, "deliver_park_giveup", _giveup)
    assert await reaper_module.redeliver_due_continuations_once() == 0
    assert seen == [("iid-r", "fp-stored")]
