"""The completion continuation ``conversation_deliver`` (``deliver_agent_completion``): a
resumed agent turn's FINAL answer minted as an already-terminal record keyed by the stable
completion id and handed to the delivery machine — no turn run, no memory append (the resumed
run already recorded the answer in its own checkpoint). It is driven with the generic
contract payload a resuming driver fires (the bound completion context merged with
``{result, completion_id, status}``). A repeated completion id dedupes to one record (the
lease-lapse re-drive is a benign no-op), a blank success answer delivers the client-safe
error reply, and an unresolvable thread raises loudly.

``status`` has exactly one success value; the explicit failure, an UNSTAMPED fire, and an
UNRECOGNIZED value all deliver the uniform client-safe notice, and each of them WARNS naming
the completion id — a resumer/delivery version skew degrades every answer to an error notice
while still returning a delivered record, so that log is its only detection.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from pydantic import BaseModel
from tai42_contract.conversations import ConversationRoute
from tai42_contract.interactions import PARK_COMPLETION_SUCCEEDED

from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import ledger as ledger_module
from tai42_skeleton.conversations import mode as mode_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import thread_lease as thread_lease_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import CompletionDeliveryError, deliver_agent_completion

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


class _EchoInput(BaseModel):
    user_message: str = ""


class FakeManager:
    def __init__(self, *routes: ConversationRoute) -> None:
        self._routes = {r.route_name: r for r in routes}

    async def list_routes(self):
        return dict(self._routes)

    async def get_route(self, name: str):
        return self._routes.get(name)


class FakeChannel:
    def __init__(self) -> None:
        self.sends: list = []

    async def notify(self, notification):
        self.sends.append(notification)
        return [f"out-{len(self.sends)}"]


class _FakeChannels:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel

    def get(self, name: str) -> FakeChannel:
        return self._channel


class _FakeApp:
    def __init__(self, channel: FakeChannel) -> None:
        self.channels = _FakeChannels(channel)


def _channel_route() -> ConversationRoute:
    return ConversationRoute(
        route_name="line",
        door="channel",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
        execution_key_fingerprint="fp-1",
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_GRACE_SECONDS", "1")
    caps_module._CAPS_CACHE.clear()
    fake = FakeRecordRedis()
    for module in (records_module, ledger_module, mode_module, thread_lease_module):
        monkeypatch.setattr(module, "client_ctx", make_record_client_ctx(fake))
    fake.seed_route("line")
    return fake


def _store() -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


async def _settle(timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = [t for t in (*turn_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS) if not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            # Recompute after the yield: a task can appear between the two checks, so wait on
            # the fresh list (asyncio.wait raises on an empty set), and return only when it
            # is still empty.
            tasks = [t for t in (*turn_module._TURN_TASKS, *delivery_module._DELIVERY_TASKS) if not t.done()]
            if not tasks:
                return
        await asyncio.wait(tasks, timeout=0.05)


def _wire(monkeypatch, manager: FakeManager, channel: FakeChannel) -> None:
    monkeypatch.setattr(turn_module, "get_conversations_manager", lambda: manager)
    monkeypatch.setattr(delivery_module, "get_conversations_manager", lambda: manager)
    monkeypatch.setattr(delivery_module, "tai42_app", _FakeApp(channel))


_TURN_LOGGER = "tai42_skeleton.conversations.turn"


def _completion_warnings(caplog, completion_id: str) -> list[str]:
    """Every delivery-tool WARNING naming ``completion_id``. A non-success fire still returns a
    delivered record, so this log is the ONLY signal that a resumer/delivery version skew is
    degrading answers into error notices."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == _TURN_LOGGER and completion_id in record.getMessage()
    ]


def _fire(thread_id: str | None, result: object, completion_id: str, status: str | None = PARK_COMPLETION_SUCCEEDED):
    """Drive the delivery tool exactly as a resuming driver does: the bound completion context
    (``{thread_id}``) merged with the terminal outcome ``{result, completion_id, status}`` — the
    generic payload shape the contract pins."""
    context = {} if thread_id is None else {"thread_id": thread_id}
    return deliver_agent_completion(**context, result=result, completion_id=completion_id, status=status)


async def test_completion_delivers_the_resumed_answer_into_the_thread(env, monkeypatch):
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    out = await _fire("bridge:line:+15550002222", "here is your answer", "cmpl-1")
    await _settle()

    message_id = out["message_id"]
    assert message_id == "cmpl-1"
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.origin == "operator"
    assert record.answer_status == "answered"
    assert record.answer == "here is your answer"
    assert record.inbound_text == ""
    assert record.caller_principal == "system:agent-resume"
    assert record.route_name == "line"
    assert record.client_address == "+15550002222"
    # Delivered from the ROUTE identity to the client address — no turn ran, no append.
    assert channel.sends[0].sender_identity == "+15550001111"
    assert channel.sends[0].recipient == "+15550002222"
    assert channel.sends[0].message == "here is your answer"
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_completion_serializes_a_structured_result(env, monkeypatch):
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    out = await _fire("bridge:line:+15550002222", {"ok": True}, "cmpl-struct")
    await _settle()

    message_id = out["message_id"]
    assert message_id == "cmpl-struct"
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == '{"ok": true}'


async def test_completion_empty_result_delivers_error_reply(env, monkeypatch):
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    out = await _fire("bridge:line:+15550002222", "   ", "cmpl-empty")
    await _settle()

    # A resumed run that produced no text delivers the SAME client-safe error reply text the
    # fresh-turn path delivers for an empty answer — never a silent non-delivery. It rides the
    # operator record as an answered reply (an operator record is always answered).
    assert out == {"message_id": "cmpl-empty"}
    record = await _store().get_record("cmpl-empty")
    assert record is not None
    assert record.answer_status == "answered"
    assert record.answer == "Sorry, something went wrong handling your message. Please try again."
    assert channel.sends[0].message == "Sorry, something went wrong handling your message. Please try again."


async def test_completion_empty_result_uses_the_route_error_reply_text_when_set(env, monkeypatch):
    # A blank resumed answer delivers the route's own ``error_reply_text`` when it carries one,
    # not the built-in default (turn.py:1501) — the SAME custom reply a fresh empty turn sends.
    spanish = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    channel = FakeChannel()
    route = _channel_route().model_copy(update={"error_reply_text": spanish})
    _wire(monkeypatch, FakeManager(route), channel)

    out = await _fire("bridge:line:+15550002222", "   ", "cmpl-empty-es")
    await _settle()

    assert out == {"message_id": "cmpl-empty-es"}
    record = await _store().get_record("cmpl-empty-es")
    assert record is not None
    assert record.answer_status == "answered"
    assert record.answer == spanish
    assert channel.sends[0].message == spanish


async def test_completion_success_with_no_result_delivers_the_notice_and_warns(env, monkeypatch, caplog):
    # A success fire carrying NO result at all: serializing it renders the literal "null", which
    # is not blank, so the blank->notice check below it would sail straight past and post the
    # word "null" into the guest's thread. It takes the client-safe notice instead, and — being
    # the same malformed-payload class the status guards catch — is announced.
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    with caplog.at_level(logging.WARNING, logger=_TURN_LOGGER):
        out = await deliver_agent_completion(
            thread_id="bridge:line:+15550002222",
            completion_id="cmpl-no-result",
            status=PARK_COMPLETION_SUCCEEDED,
        )
    await _settle()

    assert out == {"message_id": "cmpl-no-result"}
    record = await _store().get_record("cmpl-no-result")
    assert record is not None
    assert record.answer == turn_module._ERROR_ANSWER_TEXT
    assert record.answer != "null"
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]
    warnings = _completion_warnings(caplog, "cmpl-no-result")
    assert len(warnings) == 1
    assert "no result" in warnings[0]


async def test_completion_dedupes_on_repeated_completion_id(env, monkeypatch):
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    first = await _fire("bridge:line:+15550002222", "the answer", "cmpl-dupe")
    await _settle()
    # A lease-lapse re-drive fires the completion a SECOND time under the same stable id: the
    # durable record already committed, so it is a benign no-op — no second record, no second send.
    second = await _fire("bridge:line:+15550002222", "the answer", "cmpl-dupe")
    await _settle()

    assert first == {"message_id": "cmpl-dupe"}
    assert second == {"message_id": "cmpl-dupe"}
    assert len(channel.sends) == 1


async def test_completion_redelivery_without_an_address_is_the_benign_no_op(env, monkeypatch, caplog):
    # The idempotency read sits ABOVE the address guard: a re-drive whose second fire lost its
    # bound address is still a redelivery of a COMMITTED record, so it takes the benign
    # already-delivered path rather than being reported as a permanently unroutable drop.
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    first = await _fire("bridge:line:+15550002222", "the answer", "cmpl-addressless-dupe")
    await _settle()
    with caplog.at_level(logging.ERROR, logger=_TURN_LOGGER):
        second = await _fire(None, "the answer", "cmpl-addressless-dupe")
    await _settle()

    assert first == {"message_id": "cmpl-addressless-dupe"}
    assert second == {"message_id": "cmpl-addressless-dupe"}
    assert len(channel.sends) == 1
    assert not [r for r in caplog.records if r.levelno == logging.ERROR and r.name == _TURN_LOGGER]


async def test_completion_non_success_delivers_the_uniform_error_notice(env, monkeypatch, caplog):
    # A non-success terminal (failed/stopped/aborted resume) delivers the uniform client-safe
    # notice — never the raw internal detail the result carries, never silence.
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    with caplog.at_level(logging.WARNING, logger=_TURN_LOGGER):
        out = await _fire("bridge:line:+15550002222", {"detail": "boom: internal stack"}, "cmpl-fail", status="failed")
    await _settle()

    # An EXPLICIT failure is announced too: a fleet whose resumes are all failing must be
    # visible, and this record's client-safe notice is indistinguishable from a degraded one.
    warnings = _completion_warnings(caplog, "cmpl-fail")
    assert len(warnings) == 1
    assert "'failed'" in warnings[0]
    assert out == {"message_id": "cmpl-fail"}
    record = await _store().get_record("cmpl-fail")
    assert record is not None
    assert record.answer_status == "answered"
    assert record.answer == turn_module._ERROR_ANSWER_TEXT
    assert [n.message for n in channel.sends] == [turn_module._ERROR_ANSWER_TEXT]
    # The internal detail never reaches the client.
    assert "boom" not in record.answer


async def test_completion_non_success_uses_the_route_error_reply_text_when_set(env, monkeypatch):
    spanish = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    channel = FakeChannel()
    route = _channel_route().model_copy(update={"error_reply_text": spanish})
    _wire(monkeypatch, FakeManager(route), channel)

    out = await _fire("bridge:line:+15550002222", "the raw terminal", "cmpl-fail-es", status="failed")
    await _settle()

    assert out == {"message_id": "cmpl-fail-es"}
    record = await _store().get_record("cmpl-fail-es")
    assert record is not None
    assert record.answer == spanish
    assert [n.message for n in channel.sends] == [spanish]


async def test_completion_unstamped_fire_delivers_the_notice_and_warns(env, monkeypatch, caplog):
    # FAIL-SAFE: an unstamped fire (no status at all — a resumer predating the status field)
    # delivers the client-safe notice and NEVER pushes an unmapped payload through the success
    # path as if it had succeeded. Pairing a new skeleton with an old agents plugin degrades
    # EVERY answer to an error notice, and the fire still returns a delivered record — so the
    # WARNING naming the completion id and the unstamped shape is the only detection there is.
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    with caplog.at_level(logging.WARNING, logger=_TURN_LOGGER):
        out = await deliver_agent_completion(
            thread_id="bridge:line:+15550002222", result="would-deliver-if-success", completion_id="cmpl-unstamped"
        )
    await _settle()

    assert out == {"message_id": "cmpl-unstamped"}
    record = await _store().get_record("cmpl-unstamped")
    assert record is not None
    assert record.answer == turn_module._ERROR_ANSWER_TEXT
    warnings = _completion_warnings(caplog, "cmpl-unstamped")
    assert len(warnings) == 1
    assert "NO status" in warnings[0]


async def test_completion_unrecognized_status_delivers_the_notice_and_warns(env, monkeypatch, caplog):
    # A status outside the contract vocabulary is non-success, exactly like the unstamped fire:
    # a value this tool cannot read must never be delivered as though it were the answer. It is
    # the same invisible version skew, so it warns and names the value that arrived.
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    with caplog.at_level(logging.WARNING, logger=_TURN_LOGGER):
        out = await _fire("bridge:line:+15550002222", "would-deliver-if-success", "cmpl-weird", status="weird")
    await _settle()

    assert out == {"message_id": "cmpl-weird"}
    record = await _store().get_record("cmpl-weird")
    assert record is not None
    assert record.answer == turn_module._ERROR_ANSWER_TEXT
    warnings = _completion_warnings(caplog, "cmpl-weird")
    assert len(warnings) == 1
    assert "'weird'" in warnings[0]


async def test_completion_success_delivers_the_answer_and_warns_nothing(env, monkeypatch, caplog):
    # The counterpart pin: the ONE recognized success status delivers the result and is silent,
    # so the warning above discriminates a degraded fire from a healthy one.
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    with caplog.at_level(logging.WARNING, logger=_TURN_LOGGER):
        out = await _fire("bridge:line:+15550002222", "the answer", "cmpl-ok", status=PARK_COMPLETION_SUCCEEDED)
    await _settle()

    assert out == {"message_id": "cmpl-ok"}
    record = await _store().get_record("cmpl-ok")
    assert record is not None
    assert record.answer == "the answer"
    assert _completion_warnings(caplog, "cmpl-ok") == []


async def test_completion_without_a_bound_thread_is_a_logged_no_op(env, monkeypatch, caplog):
    # A fire whose completion binding carried NO address: nothing reverses a completion id to a
    # thread, so it is a LOGGED no-op — never a TypeError the resumer would retry forever on a
    # permanently unroutable fire.
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    with caplog.at_level(logging.ERROR, logger=_TURN_LOGGER):
        out = await _fire(None, "the orphaned answer", "cmpl-no-thread")
        # A BLANK address is the same non-address, guarded on falsiness exactly as the
        # completion-id guard is — never a thread id the reversal would then reject.
        blank = await deliver_agent_completion(thread_id="", result="also orphaned", completion_id="cmpl-blank-thread")
    await _settle()

    assert out == {"message_id": None}
    assert blank == {"message_id": None}
    assert await _store().get_record("cmpl-no-thread") is None
    assert await _store().get_record("cmpl-blank-thread") is None
    assert channel.sends == []
    assert any("cmpl-no-thread" in record.getMessage() for record in caplog.records)
    assert any("cmpl-blank-thread" in record.getMessage() for record in caplog.records)


async def test_completion_without_a_completion_id_raises(env, monkeypatch):
    # The idempotency id is the exactly-once key; a fire without one is a malformed payload, not
    # something to deliver under a guessed id.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)

    with pytest.raises(ValueError, match="completion_id"):
        await deliver_agent_completion(thread_id="bridge:line:+15550002222", result="hi")
    assert channel.sends == []


async def test_completion_unresolvable_thread_raises(env, monkeypatch):
    channel = FakeChannel()
    route = _channel_route()
    _wire(monkeypatch, FakeManager(route), channel)

    # A non-bridge thread cannot be reversed to a route + address.
    with pytest.raises(CompletionDeliveryError, match="not a reserved bridge thread"):
        await _fire("not-a-bridge-thread", "hi", "cmpl-bad-1")
    # A route-keyed thread whose route no longer exists.
    with pytest.raises(CompletionDeliveryError, match="no longer exists"):
        await _fire("bridge:gone:+15550002222", "hi", "cmpl-bad-2")
    assert channel.sends == []
