"""The authed API door: the sync-wait/callback split, the signed callback, and the caller identity model."""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.agent import Agent
from tai42_contract.template import TemplatedText

from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import intake as intake_module
from tai42_skeleton.conversations.turn import keys as keys_module

from .conftest import (
    EchoAgent,
    FakeChannel,
    FakeManager,
    MemoryAgent,
    _accepting_callback,
    _all_record_ids,
    _api_route,
    _api_route_no_callback,
    _channel_route,
    _connected,
    _EchoInput,
    _settle,
    _store,
    _tool_api_route,
    _wire,
    _wire_tool,
    rendered_user_message,
)


async def test_api_wait_fast_returns_answer_and_suppresses_callback(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message(
        "chat", "user-7", "hello", "alice", wait_seconds=5, client_connected=_connected
    )
    await _settle()

    assert result.answer is not None
    assert result.answer.answer == "echo: hello"
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.caller_principal == "alice"
    assert record.delivery_status is DeliveryStatus.DELIVERED
    # The sync-wait delivered it, so NO callback was POSTed (no double-fire).
    assert posted == []


async def test_an_api_error_outcome_carries_the_route_error_reply_text(env, monkeypatch):
    # An api-door error surfaces the route's ``error_reply_text`` in the sync-wait
    # ConversationAnswer: the caller sees the custom reply, not the built-in default.
    spanish = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    _wire(monkeypatch, FakeManager(_tool_api_route(error_reply_text=spanish)))

    def _boom(kw):
        raise RuntimeError("tool blew up")

    _wire_tool(monkeypatch, _boom)

    async def _post(url, body, signature, timeout_seconds):
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message(
        "tool-api", "user-7", "hi", "alice", wait_seconds=5, client_connected=_connected
    )
    await _settle()

    assert result.answer is not None
    assert result.answer.status == "error"
    assert result.answer.answer == spanish
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert "tool blew up" in record.error


async def test_api_no_wait_returns_202_then_posts_signed_callback(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append((url, signature))
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message(
        "chat", "user-7", "hello", "alice", wait_seconds=0, client_connected=_connected
    )
    assert result.answer is None  # 202
    await _settle()

    assert len(posted) == 1
    assert posted[0][0] == "https://cb.example/x"
    assert posted[0][1].startswith("sha256=")
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_api_slow_wait_returns_202_then_posts_the_callback(env, monkeypatch):
    # A turn that does NOT finish inside the wait window falls back to 202 and its answer
    # is delivered by exactly one callback — the timeout arm of the wait, then the
    # not-yet-done arm of _deliver_when_done, with no double-fire against the sweep.
    _wire(monkeypatch, FakeManager(_api_route()))
    release = asyncio.Event()

    class _SlowAgent(Agent):
        tool_name = "slow"
        ToolInput = _EchoInput

        async def run(self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs):
            await release.wait()
            text = rendered_user_message(user_message)
            return f"echo: {text}"

    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": _SlowAgent()})
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message(
        "chat", "user-7", "hello", "alice", wait_seconds=1, client_connected=_connected
    )
    assert result.answer is None  # the wait elapsed before the turn finished: 202

    release.set()
    await _settle()

    assert posted == ["https://cb.example/x"]  # exactly one callback, no double-fire
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_api_no_callback_slow_wait_202_then_readable_by_poll_and_no_callback(env, monkeypatch):
    # A poll-only api route (no callback declared): a turn that outruns the wait falls back
    # to 202, its answer is served terminal-readable from the poll door, and NO callback is
    # ever attempted — the exact path a caller using only sync/poll takes.
    from types import SimpleNamespace

    from tai42_skeleton.operations import conversations as ops_module

    route = _api_route_no_callback()
    _wire(monkeypatch, FakeManager(route))
    monkeypatch.setattr(ops_module, "get_conversations_manager", lambda: FakeManager(route))

    async def _caller():
        return SimpleNamespace(is_admin=False)

    monkeypatch.setattr(ops_module, "resolve_caller", _caller)

    release = asyncio.Event()

    class _SlowAgent(Agent):
        tool_name = "slow"
        ToolInput = _EchoInput

        async def run(self, *, user_message: TemplatedText | None = None, thread_id: str | None = None, **kwargs):
            await release.wait()
            text = rendered_user_message(user_message)
            return f"echo: {text}"

    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": _SlowAgent()})
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message(
        "chat", "user-7", "hello", "alice", wait_seconds=1, client_connected=_connected
    )
    assert result.answer is None  # the wait elapsed before the turn finished: 202

    release.set()
    await _settle()

    assert posted == []  # a poll-only route never POSTs a callback
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    # The consumer's seam: the poll door serves the answer back off the terminal record.
    view = await ops_module.get_conversation_message("chat", result.message_id)
    assert view["answer"] == "echo: hello"
    assert view["delivery_status"] == DeliveryStatus.DELIVERED.value


# -- the inline-200 precondition: a receiver still connected --------------------


async def _gone() -> bool:
    return False


async def test_api_wait_gone_client_falls_to_the_signed_callback(env, monkeypatch):
    # The turn finishes inside the window but the client has hung up (probe False): the inline
    # claim is NOT taken, so the response carries no inline answer and the answer is delivered
    # by exactly one signed callback — never a write to a closed socket.
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append((url, signature))
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message(
        "chat", "user-7", "hello", "alice", wait_seconds=5, client_connected=_gone
    )
    assert result.answer is None  # no inline answer written to a gone client
    await _settle()

    assert len(posted) == 1  # delivered by exactly one signed callback
    assert posted[0][1].startswith("sha256=")
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_api_wait_gone_client_poll_only_is_terminal_readable_and_posts_nothing(env, monkeypatch):
    # A gone client on a poll-only route (no callback declared): the answer is not written inline
    # and no callback is POSTed; the record is driven terminal and served back off the poll door.
    from types import SimpleNamespace

    from tai42_skeleton.operations import conversations as ops_module

    route = _api_route_no_callback()
    _wire(monkeypatch, FakeManager(route))
    monkeypatch.setattr(ops_module, "get_conversations_manager", lambda: FakeManager(route))

    async def _caller():
        return SimpleNamespace(is_admin=False)

    monkeypatch.setattr(ops_module, "resolve_caller", _caller)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message(
        "chat", "user-7", "hello", "alice", wait_seconds=5, client_connected=_gone
    )
    assert result.answer is None
    await _settle()

    assert posted == []  # a poll-only route never POSTs a callback
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    view = await ops_module.get_conversation_message("chat", result.message_id)
    assert view["answer"] == "echo: hello"
    assert view["delivery_status"] == DeliveryStatus.DELIVERED.value


async def test_api_wait_zero_never_evaluates_the_probe(env, monkeypatch):
    # The pure-async path has no sync wait to gate, so it must never touch the probe.
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    async def _post(url, body, signature, timeout_seconds):
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    async def _must_not_run() -> bool:
        raise AssertionError("the probe must not be evaluated when wait_seconds == 0")

    result = await turn_module.submit_api_message(
        "chat", "user-7", "hello", "alice", wait_seconds=0, client_connected=_must_not_run
    )
    assert result.answer is None  # 202
    await _settle()


async def test_api_wait_probe_that_raises_propagates(env, monkeypatch):
    # A probe that raises is a real fault, never a silent "connected": it propagates.
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    async def _post(url, body, signature, timeout_seconds):
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    class _ProbeError(RuntimeError):
        pass

    async def _boom() -> bool:
        raise _ProbeError("probe failed")

    with pytest.raises(_ProbeError):
        await turn_module.submit_api_message("chat", "user-7", "hello", "alice", wait_seconds=5, client_connected=_boom)
    await _settle()


async def test_api_callback_retries_then_fails(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_BASE_SECONDS", "0.01")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_MAX_SECONDS", "0.01")
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 500

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    result = await turn_module.submit_api_message(
        "chat", "user-7", "hello", "alice", wait_seconds=0, client_connected=_connected
    )
    await _settle()

    assert len(posted) == 2  # exhausted delivery_max_attempts
    record = await _store().get_record(result.message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.FAILED


async def test_a_caller_cannot_reach_another_callers_thread_by_naming_its_end_user(env, monkeypatch):
    # The api door's end-user id is caller-asserted, so the thread it keys must also carry
    # the AUTHENTICATED caller: otherwise one caller reads another's conversation memory
    # back out of the agent's own answer.
    agent = MemoryAgent()
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    alice = await turn_module.submit_api_message(
        "chat", "shared-user", "my lucky number is 4111", "alice", 5, client_connected=_connected
    )
    await _settle()
    bob = await turn_module.submit_api_message(
        "chat", "shared-user", "what did I say?", "bob", 5, client_connected=_connected
    )
    await _settle()

    assert alice.thread_id != bob.thread_id
    assert alice.thread_id == "bridge:chat:alice/shared-user"
    assert bob.thread_id == "bridge:chat:bob/shared-user"
    # Two threads means two memories: bob's turn never saw alice's message.
    assert sorted(agent.threads) == [alice.thread_id, bob.thread_id]
    assert bob.answer is not None
    assert bob.answer.answer is not None
    assert "4111" not in bob.answer.answer
    # The record's address matches the thread it ran on, so the two never disagree.
    record = await _store().get_record(bob.message_id)
    assert record is not None
    assert record.client_address == "bob/shared-user"


def test_the_api_address_join_is_unambiguous_for_every_principal():
    # The principal is percent-encoded, so no principal/end-user pair can spell the same
    # address slot as a different pair — the separator is not forgeable.
    compose = keys_module._api_client_address
    assert compose("a/b", "c") != compose("a", "b/c")
    assert compose("a%2Fb", "c") != compose("a/b", "c")
    assert compose("a:b", "c") != compose("a", "b:c")


async def test_an_api_caller_cannot_outrun_its_cap_by_varying_the_end_user_id(env, monkeypatch):
    # The cap keys on the caller, not on the caller-chosen end-user id, so minting a fresh
    # id per message buys no extra turns.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "2")
    caps_module._CAPS_CACHE.clear()
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())
    store = _store()

    for index in range(2):
        await turn_module.submit_api_message("chat", f"u-{index}", "hi", "alice", 0, client_connected=_connected)
    await _settle()

    with pytest.raises(caps_module.AddressRateLimitedError, match="alice"):
        await turn_module.submit_api_message("chat", "u-2", "hi", "alice", 0, client_connected=_connected)
    # The refusal wrote nothing: only the two admitted messages left records.
    assert len(await _all_record_ids(store)) == 2

    # A different caller has its own budget.
    await turn_module.submit_api_message("chat", "u-0", "hi", "bob", 0, client_connected=_connected)
    await _settle()
    assert len(await _all_record_ids(store)) == 3


async def test_an_api_caller_cannot_drain_a_channel_addresss_bucket(env, monkeypatch):
    # The api door's bucket names the caller and the channel door's names the route and
    # the provider-attested address, so an authed caller cannot spend a phone user's budget.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(), _api_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    # The caller spends its own single token naming the phone number as its end user.
    await turn_module.submit_api_message("chat", "+15550002222", "hi", "alice", 0, client_connected=_connected)
    await _settle()
    with pytest.raises(caps_module.AddressRateLimitedError):
        await turn_module.submit_api_message("chat", "+15550002222", "again", "alice", 0, client_connected=_connected)

    # The real phone user is untouched: it is admitted and answered.
    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hello", "PID1")
    await _settle()
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.answer == "echo: hello"
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_two_routes_sharing_an_address_have_independent_buckets(env, monkeypatch):
    # The bucket key carries the route, so draining one route's budget for an address
    # leaves the same address on another route fully funded.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    agent = EchoAgent()
    channel = FakeChannel()
    other = _channel_route(route_name="line-b", our_identity="+15550009999")
    _wire(monkeypatch, FakeManager(_channel_route(), other), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "one", "PID1")
    await _settle()
    # The address is now over its cap ON THAT ROUTE: the next message buys a slow-down reply.
    shed = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2")
    await _settle()
    shed_record = await store.get_record(shed)
    assert shed_record is not None
    assert shed_record.answer == intake_module._SLOW_DOWN_TEXT

    # The same address on the other route still gets its own turn.
    admitted = await turn_module.accept("twilio", "+15550009999", "+15550002222", "+15550002222", "three", "PID3")
    await _settle()
    record = await store.get_record(admitted)
    assert record is not None
    assert record.answer == "echo: three"
    assert record.thread_id == "bridge:line-b:+15550002222"


async def test_two_addresses_sharing_a_cap_key_share_one_turn_bucket(env, monkeypatch):
    # The bridge half of the web rotate-reset defense: two DIFFERENT conversation
    # identities that name the SAME accountable cap key share ONE turn bucket, so a
    # second identity minted under that key cannot buy itself a fresh budget.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    first = await turn_module.accept("twilio", "+15550001111", "visitor-A", "net-bucket", "one", "PID1")
    await _settle()
    first_record = await store.get_record(first)
    assert first_record is not None
    assert first_record.answer == "echo: one"

    # A DIFFERENT identity under the SAME cap key is already over the shared cap: it sheds.
    shed = await turn_module.accept("twilio", "+15550001111", "visitor-B", "net-bucket", "two", "PID2")
    await _settle()
    shed_record = await store.get_record(shed)
    assert shed_record is not None
    assert shed_record.answer == intake_module._SLOW_DOWN_TEXT
    # ...and the two really are distinct conversations.
    assert first_record.client_address == "visitor-A"
    assert shed_record.client_address == "visitor-B"


async def test_two_api_routes_give_a_caller_independent_buckets(env, monkeypatch):
    # The api bucket key carries the route, so a caller draining its cap on one route
    # still has a full budget on another — the route qualifier the caller-scoped fix added.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    _wire(monkeypatch, FakeManager(_api_route("chat"), _api_route("account")))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    await turn_module.submit_api_message("chat", "u-1", "hi", "alice", 0, client_connected=_connected)
    await _settle()
    # alice has spent her single token on ``chat``; a second there is refused.
    with pytest.raises(caps_module.AddressRateLimitedError, match="chat"):
        await turn_module.submit_api_message("chat", "u-2", "hi", "alice", 0, client_connected=_connected)

    # ``account``'s budget for alice is untouched: her message there is admitted and answered.
    result = await turn_module.submit_api_message("account", "u-1", "hi", "alice", 5, client_connected=_connected)
    await _settle()
    assert result.answer is not None
    assert result.answer.answer == "echo: hi"


async def test_an_api_route_override_raises_the_cap_and_the_503_quotes_it(env, monkeypatch):
    # A route carrying ``turns_per_hour_override`` runs its callers at that rate, above the
    # global cap of 1, and the refusal names the EFFECTIVE (override) rate, not the global.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    _wire(monkeypatch, FakeManager(_api_route("chat", turns_per_hour_override=3)))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())

    # The override grants three turns where the global cap would have allowed one.
    for index in range(3):
        await turn_module.submit_api_message("chat", f"u-{index}", "hi", "alice", 0, client_connected=_connected)
    await _settle()

    # The fourth is refused, and the 503 quotes the override rate of 3/hour, not the global 1.
    with pytest.raises(caps_module.AddressRateLimitedError, match="3/hour"):
        await turn_module.submit_api_message("chat", "u-3", "hi", "alice", 0, client_connected=_connected)


async def test_a_channel_route_override_raises_the_cap_through_the_accept_path(env, monkeypatch):
    # The channel door honours ``turns_per_hour_override`` the same way the api door does: an
    # address on an overridden channel route is admitted beyond the global budget, and the
    # paid slow-down reply fires only once the OVERRIDE budget (not the global one) is spent.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(turns_per_hour_override=3)), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    # The override grants three turns where the global cap of 1 would have shed the second.
    for index in range(3):
        admitted = await turn_module.accept(
            "twilio", "+15550001111", "+15550002222", "+15550002222", f"msg-{index}", f"PID{index}"
        )
        await _settle()
        record = await store.get_record(admitted)
        assert record is not None
        assert record.answer == f"echo: msg-{index}"

    # Only the fourth message — past the override budget, not the global one — buys the
    # slow-down reply.
    shed = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "over", "PID9")
    await _settle()
    shed_record = await store.get_record(shed)
    assert shed_record is not None
    assert shed_record.answer == intake_module._SLOW_DOWN_TEXT


@pytest.mark.parametrize("principal", [None, "", "   "])
async def test_an_api_message_without_an_authenticated_caller_is_refused(env, monkeypatch, principal):
    # Keying a thread or a bucket on an absent principal would pool every anonymous caller
    # into one shared conversation, so the door refuses instead.
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    with pytest.raises(turn_module.UnauthenticatedApiCallerError):
        await turn_module.submit_api_message("chat", "u-7", "hi", principal, 5, client_connected=_connected)
    await _settle()

    assert agent.calls == []
    assert await _all_record_ids(_store()) == []
    assert caps_module.get_turn_caps()._thread_waiters == {}


async def test_api_callback_signature_verifies_under_the_row_secret(env, monkeypatch):
    import hashlib
    import hmac

    _wire(monkeypatch, FakeManager(_api_route()))
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    captured: list[tuple[bytes, str]] = []

    async def _post(url, body, signature, timeout_seconds):
        captured.append((body, signature))
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)

    await turn_module.submit_api_message(
        "chat", "user-7", "hello", "alice", wait_seconds=0, client_connected=_connected
    )
    await _settle()

    assert len(captured) == 1
    body, signature = captured[0]
    # A receiver recomputes HMAC-SHA256(callback_secret, raw_body) and compares — the
    # signature the executor sent verifies under the route's secret and nothing else.
    expected = "sha256=" + hmac.new(b"sec-1", body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature, expected)
    forged = "sha256=" + hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()
    assert not hmac.compare_digest(signature, forged)
