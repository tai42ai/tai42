"""The interactions coroutines a tool run reaches hold no settings instance across an await.

A tool run's supervisor is a root task that can outlive the serving generation that
admitted it, so every interactions coroutine the run reaches — the shared visit and its
helpers, the ask, the park teardown and lookup helpers, the interaction operations a run
can execute as tool bodies, the channel sinks a run's notify reaches — reads its
configuration through the cached accessor at the point of use and keeps only what it
derives (a store, a connection). Each case parks one coroutine at an await through the
REAL cached accessor, retires the generation the way the reload's retire step does
(advance the epoch, reset, sweep), then lets the coroutine finish.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import tai42_kit.clients as kit_clients
from tai42_contract.channels import Channel, ChannelDelivery
from tai42_contract.conversations import DeliveryReceipt
from tai42_contract.interactions import ResumeItem, SuspendedInteraction, TakeItem
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_kit.clients.base import advance_client_epoch
from tai42_kit.settings import reset_all_settings, sweep_stale_settings
from tai42_kit.utils.state_context import state_context

from tai42_skeleton.channels import notifications_sink as notifications_sink_module
from tai42_skeleton.channels import send_receipts as send_receipts_module
from tai42_skeleton.channels.settings import channels_settings
from tai42_skeleton.interactions import authorization as authorization_module
from tai42_skeleton.interactions import checkpoint_liveness as checkpoint_liveness_module
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions import kill as kill_module
from tai42_skeleton.interactions import reaction as reaction_module
from tai42_skeleton.interactions import visit as visit_module
from tai42_skeleton.interactions.ask import delivery as delivery_module
from tai42_skeleton.interactions.ask.timing import DeadlineWindow
from tai42_skeleton.interactions.settings import interactions_settings
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.operations import interactions as interaction_ops
from tai42_skeleton.tools import platform_referees as referees_module

from .._fakes.interactions_redis import FakeRedis
from ._continuation_support import configure_interactions_store

# The settings types a run's interactions coroutines read through their cached accessors.
_HELD_TYPES = (".InteractionsSettings", ".ChannelsSettings")

_CANDIDATES = SubjectCandidates(target_kind="tool", target_name="t", by_kind={"person": "p1"})


def _entry(status: str) -> dict[str, Any]:
    """One parked-list entry the visit pre-check reads (``i-1`` on group ``g-1``)."""
    return {
        "id": "i-1",
        "group_id": "g-1",
        "status": status,
        "to": "caller",
        "answer_format": "text",
        "format_payload": None,
        "asked_by": [],
    }


@pytest.fixture
async def gate() -> asyncio.Future[None]:
    return asyncio.get_running_loop().create_future()


@pytest.fixture
def parked_at(monkeypatch: pytest.MonkeyPatch, gate: asyncio.Future[None]) -> Callable[..., None]:
    """Replace ``owner.name`` with a coroutine that waits on the gate, then returns ``value``."""

    def _park(owner: object, name: str, value: Any = None) -> None:
        async def _parked(*args: Any, **kwargs: Any) -> Any:
            await gate
            return value

        monkeypatch.setattr(owner, name, _parked, raising=False)

    return _park


@pytest.fixture
def returning(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Replace ``owner.name`` with a coroutine that returns ``value`` at once."""

    def _return(owner: object, name: str, value: Any) -> None:
        async def _returned(*args: Any, **kwargs: Any) -> Any:
            return value

        monkeypatch.setattr(owner, name, _returned)

    return _return


@pytest.fixture(autouse=True)
def real_accessor(monkeypatch: pytest.MonkeyPatch, fake_client_ctx) -> None:
    """Configure the store, read settings through the real cached (epoch-stamped) accessors."""
    configure_interactions_store(monkeypatch)
    interactions_settings.cache_clear()
    channels_settings.cache_clear()
    for module in (
        visit_module,
        helper_module,
        kill_module,
        reaction_module,
        authorization_module,
        checkpoint_liveness_module,
        interaction_ops,
        send_receipts_module,
        notifications_sink_module,
    ):
        monkeypatch.setattr(module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(kit_clients, "client_ctx", fake_client_ctx)


async def _held_while_parked(
    door: Callable[[], Coroutine[Any, Any, Any]],
    gate: asyncio.Future[None],
    caplog: pytest.LogCaptureFixture,
) -> list[Any]:
    """Run ``door`` until it parks on ``gate``, retire the settings generation, sweep, then finish it."""
    task = asyncio.create_task(door())
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done(), task.exception() if task.done() else None
    try:
        retired = advance_client_epoch()
        reset_all_settings()
        with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
            held = [h for h in sweep_stale_settings(retired) if h.settings_type.endswith(_HELD_TYPES)]
    finally:
        gate.set_result(None)
        with suppress(Exception):
            await task
    return held


def _assert_nothing_held(held: list[Any], caplog: pytest.LogCaptureFixture) -> None:
    assert held == []
    assert "InteractionsSettings" not in caplog.text
    assert "ChannelsSettings" not in caplog.text


def _visit(**kwargs: Any) -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _door() -> Any:
        with state_context(StateContext(door="api", candidates=_CANDIDATES)):
            return await visit_module.visit(
                target_name="t",
                cancel=kwargs.get("cancel", []),
                resume=kwargs.get("resume", []),
                start=kwargs.get("start"),
                extras={},
            )

    return _door


async def test_a_visit_suspended_in_its_start_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture, gate: asyncio.Future[None]
) -> None:
    async def _start(extras: Any) -> str:
        await gate
        return "done"

    _assert_nothing_held(await _held_while_parked(_visit(start=_start), gate, caplog), caplog)


async def test_a_visit_suspended_on_its_parked_list_read_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture, gate: asyncio.Future[None], parked_at: Callable[..., None]
) -> None:
    parked_at(InteractionStore, "list_parked_for", [])
    held = await _held_while_parked(_visit(cancel=["i-1"]), gate, caplog)
    _assert_nothing_held(held, caplog)


async def test_a_visit_suspended_in_a_cancel_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture,
    gate: asyncio.Future[None],
    parked_at: Callable[..., None],
    returning: Callable[..., None],
) -> None:
    returning(InteractionStore, "list_parked_for", [_entry("asking")])
    parked_at(visit_module, "kill_park", "killed")
    held = await _held_while_parked(_visit(cancel=["i-1"]), gate, caplog)
    _assert_nothing_held(held, caplog)


async def test_a_visit_suspended_in_a_take_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture,
    gate: asyncio.Future[None],
    parked_at: Callable[..., None],
    returning: Callable[..., None],
) -> None:
    returning(InteractionStore, "list_parked_for", [_entry("finished")])
    parked_at(InteractionStore, "claim_outcome", None)
    held = await _held_while_parked(_visit(resume=[TakeItem(id="i-1")]), gate, caplog)
    _assert_nothing_held(held, caplog)


async def test_a_visit_suspended_in_a_resume_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture,
    gate: asyncio.Future[None],
    parked_at: Callable[..., None],
    returning: Callable[..., None],
) -> None:
    returning(InteractionStore, "list_parked_for", [_entry("asking")])
    parked_at(InteractionStore, "get_state", None)
    held = await _held_while_parked(_visit(resume=[ResumeItem(id="i-1", payload="yes")]), gate, caplog)
    _assert_nothing_held(held, caplog)


async def test_a_visit_suspended_normalising_a_caller_park_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture, gate: asyncio.Future[None], parked_at: Callable[..., None]
) -> None:
    parked_at(InteractionStore, "list_parked_for", [])

    async def _start(extras: Any) -> SuspendedInteraction:
        return SuspendedInteraction(interaction_id="i-1", interaction_ids=["i-1"], caller_interaction_ids=["i-1"])

    _assert_nothing_held(await _held_while_parked(_visit(start=_start), gate, caplog), caplog)


async def test_normalise_started_suspended_on_the_store_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture, gate: asyncio.Future[None], parked_at: Callable[..., None]
) -> None:
    parked_at(InteractionStore, "list_parked_for", [])
    sentinel = SuspendedInteraction(interaction_id="i-1", interaction_ids=["i-1"], caller_interaction_ids=["i-1"])

    async def _door() -> Any:
        with state_context(StateContext(door="api", candidates=_CANDIDATES)):
            return await visit_module.normalise_started(sentinel)

    _assert_nothing_held(await _held_while_parked(_door, gate, caplog), caplog)


async def test_list_parked_for_suspended_on_the_store_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture, gate: asyncio.Future[None], parked_at: Callable[..., None]
) -> None:
    parked_at(InteractionStore, "list_parked_for", [])

    async def _door() -> Any:
        return await visit_module.list_parked_for(StateContext(door="api", candidates=_CANDIDATES))

    _assert_nothing_held(await _held_while_parked(_door, gate, caplog), caplog)


def _sync_ask() -> Callable[[], Coroutine[Any, Any, Any]]:
    async def _door() -> Any:
        return await helper_module.ask("proceed?", timeout=30)

    return _door


async def test_a_sync_ask_suspended_persisting_its_question_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture, gate: asyncio.Future[None], parked_at: Callable[..., None]
) -> None:
    parked_at(InteractionStore, "reserve_open_slot", False)
    _assert_nothing_held(await _held_while_parked(_sync_ask(), gate, caplog), caplog)


async def test_a_sync_ask_suspended_waiting_for_its_answer_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture,
    gate: asyncio.Future[None],
    parked_at: Callable[..., None],
    returning: Callable[..., None],
) -> None:
    returning(InteractionStore, "reserve_open_slot", True)
    returning(InteractionStore, "add", None)
    returning(InteractionStore, "prune_pending", "pruned")
    parked_at(InteractionStore, "wait_for_reply", None)
    _assert_nothing_held(await _held_while_parked(_sync_ask(), gate, caplog), caplog)


async def test_a_sync_ask_suspended_pruning_its_question_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture,
    gate: asyncio.Future[None],
    parked_at: Callable[..., None],
    returning: Callable[..., None],
) -> None:
    returning(InteractionStore, "reserve_open_slot", True)
    returning(InteractionStore, "add", None)
    returning(InteractionStore, "wait_for_reply", None)
    parked_at(InteractionStore, "prune_pending", "pruned")
    _assert_nothing_held(await _held_while_parked(_sync_ask(), gate, caplog), caplog)


async def test_a_channel_delivery_suspended_in_its_send_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture, gate: asyncio.Future[None]
) -> None:
    class _ParkedChannel:
        async def deliver(self, delivery: ChannelDelivery) -> None:
            await gate

    async def _door() -> None:
        now = datetime.now(UTC)
        window = DeadlineWindow(
            budget=30,
            created_at=now,
            timeout_at=now + timedelta(seconds=30),
            deadline=asyncio.get_running_loop().time() + 30,
            park_ttl_margin_seconds=0,
        )
        await delivery_module.deliver_with_retry(
            cast(Channel, _ParkedChannel()),
            cast(ChannelDelivery, object()),
            InteractionStore("interactions:"),
            window,
            channel="chat-line",
            recipient="r-1",
            interaction_id="i-1",
            group="g-1",
            question="proceed?",
            sensitive=False,
        )

    _assert_nothing_held(await _held_while_parked(_door, gate, caplog), caplog)


@pytest.mark.parametrize(
    ("door", "owner", "first_await", "value"),
    [
        (lambda: helper_module.cancel_parks_for_thread("th-1"), InteractionStore, "thread_park_members", []),
        (lambda: helper_module.cancel_parks_for_person("p-1"), InteractionStore, "thread_park_members", []),
        (lambda: helper_module.rekey_parks_for_merge("p-1", "p-2"), InteractionStore, "rekey_subject", []),
        (
            lambda: kill_module.kill_parks_for_subject("person", "p-1", reason="erased"),
            InteractionStore,
            "subject_members",
            [],
        ),
        (lambda: reaction_module.react("i-1", {"kind": "change"}, {}), InteractionStore, "get_state", None),
        (lambda: authorization_module._stored_run_delivery_id("i-1"), InteractionStore, "get_state", None),
        (
            lambda: checkpoint_liveness_module.threads_with_live_parks("memory", None, ["th-1"]),
            InteractionStore,
            "thread_park_members",
            [],
        ),
        (
            lambda: referees_module._parked_interaction_referee("t"),
            InteractionStore,
            "parked_continuation_tools",
            [],
        ),
        (lambda: interaction_ops.answer_interaction("i-1", "yes"), InteractionStore, "get_state", None),
        (lambda: interaction_ops.react_interaction("i-1", {"kind": "change"}, {}), InteractionStore, "get_state", None),
        (lambda: interaction_ops.cancel_interaction("i-1"), InteractionStore, "get_state", None),
        (lambda: interaction_ops.list_interactions(), InteractionStore, "pending", []),
        (lambda: interaction_ops.list_pending_interactions(), InteractionStore, "list_pending", []),
        (
            lambda: send_receipts_module.index_send("chat-line", ["m-1"], trace_id="t", span_id="s"),
            FakeRedis,
            "set",
            None,
        ),
        (
            lambda: send_receipts_module.record_send_receipt("chat-line", "m-1", DeliveryReceipt.DELIVERED),
            FakeRedis,
            "get",
            None,
        ),
        (
            lambda: notifications_sink_module.record_notification("hello"),
            notifications_sink_module.NotificationSink,
            "record",
            {},
        ),
        (
            lambda: notifications_sink_module.read_notifications(),
            notifications_sink_module.NotificationSink,
            "read",
            [],
        ),
    ],
    ids=[
        "cancel_parks_for_thread",
        "cancel_parks_for_person",
        "rekey_parks_for_merge",
        "kill_parks_for_subject",
        "react",
        "stored_run_delivery_id",
        "threads_with_live_parks",
        "parked_interaction_referee",
        "answer_interaction",
        "react_interaction",
        "cancel_interaction",
        "list_interactions",
        "list_pending_interactions",
        "index_send",
        "record_send_receipt",
        "record_notification",
        "read_notifications",
    ],
)
async def test_an_interactions_coroutine_suspended_on_the_store_holds_no_retired_settings(
    caplog: pytest.LogCaptureFixture,
    gate: asyncio.Future[None],
    parked_at: Callable[..., None],
    door: Callable[[], Coroutine[Any, Any, Any]],
    owner: object,
    first_await: str,
    value: Any,
) -> None:
    parked_at(owner, first_await, value)
    _assert_nothing_held(await _held_while_parked(door, gate, caplog), caplog)
