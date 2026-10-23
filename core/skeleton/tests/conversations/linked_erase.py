"""The shared world of the absorbed-person erase tests: persons on one multichannel target, their
conversations, their parks and state, a link between two of them, and a scan of every store.

The conversations stores run the real script text on ``fakeredis[lua]``; the states service is the
real one over the in-memory fake Postgres; the interactions store is its fake Redis; the checkpoint
provider records every thread it was asked to forget. A link is the redeem's store steps: the merge,
then the re-key of the absorbed person's parks onto the survivor.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from tai42_contract.channels import ChannelAddress, ChannelDeliveryError, SendOutcome
from tai42_contract.conversations import AnswerPart, ConversationRoute, Person, PersonAddress, TargetConversationConfig

from tai42_skeleton.channels import notify as notify_module
from tai42_skeleton.channels.notify import notify_user
from tai42_skeleton.conversations.models import AskSend, ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.persons import PairingTarget, _address_key_of
from tai42_skeleton.conversations.sends import SendAttempts, recorded_send
from tai42_skeleton.conversations.thread_ids import person_thread_id, route_thread_id
from tai42_skeleton.interactions.helper import rekey_park_subjects

from ..states.conftest import FakeStatesPg
from .conftest import FakeChannel, LuaEnv, RecordingSaver, _api_route, _channel_route, create_record

STATE = "assistant_memory"
TARGET = PairingTarget(target_kind="agent", target_name="echo")
_TARGET_KEY = (TARGET.target_kind, TARGET.target_name)
LINE_IDENTITY = "+15550001111"
LINE_B_IDENTITY = "+15550009999"
LINE = _channel_route("line", LINE_IDENTITY)
LINE_B = _channel_route("line-b", LINE_B_IDENTITY)
CHAT = _api_route("chat")
CHAT_2 = _api_route("chat-2")


def channel_address(route: ConversationRoute, address: str) -> PersonAddress:
    """``address`` on the channel route ``route``, as the person store keeps it."""
    return PersonAddress(
        door="channel",
        routes=[route.route_name],
        channel=route.channel,
        our_identity=route.our_identity,
        address=address,
        linked_at=datetime.now(UTC),
    )


def api_address(address: str, *routes: ConversationRoute) -> PersonAddress:
    """The api ``address`` that wrote under ``routes``."""
    return PersonAddress(
        door="api",
        routes=[route.route_name for route in routes],
        channel=None,
        our_identity=None,
        address=address,
        linked_at=datetime.now(UTC),
    )


def person(person_id: str, *addresses: PersonAddress, age_days: int = 0) -> Person:
    """A person of the target with ``addresses``, created ``age_days`` days ago."""
    return Person(
        person_id=person_id,
        target_kind=TARGET.target_kind,
        target_name=TARGET.target_name,
        created_at=datetime.now(UTC) - timedelta(days=age_days),
        addresses=list(addresses),
    )


def state_row(kind: str, key: str) -> tuple[str, str, str, str, str]:
    """The states store key of the target's declared state on the subject ``(kind, key)``."""
    return (STATE, *_TARGET_KEY, kind, key)


def _park_request(store: Any, interaction_id: str, person_id: str | None):
    from tai42_contract.interactions import AnswerFormat, InteractionRequest
    from tai42_contract.states import StateContext, SubjectCandidates

    now = datetime.now(UTC)
    expiry = now + timedelta(hours=1)
    context = None
    if person_id is not None:
        candidates = SubjectCandidates(
            target_kind=TARGET.target_kind, target_name=TARGET.target_name, by_kind={"person": person_id}
        )
        context = StateContext(door="conversation", candidates=candidates)
    return InteractionRequest(
        interaction_id=interaction_id,
        group_id=f"g-{interaction_id}",
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(interaction_id),
        created_at=now,
        timeout_at=expiry,
        mode="async",
        continuation_tool="resume_tool",
        continuation_identity="svc-key",
        continuation_state_context=context,
        expiry_at=expiry,
    )


@dataclass
class HeldSend:
    """A recorded ``notify`` send held inside its medium call until the test answers it."""

    message_id: str
    task: asyncio.Future[Any]
    medium: asyncio.Future[SendOutcome]

    async def answer(self, address: str, our_identity: str) -> None:
        """The medium accepts the send for ``address`` from ``our_identity``."""
        self.medium.set_result(SendOutcome(our_identity=our_identity, address=address, message_ids=["held-1"]))
        await self.task

    async def fail(self) -> None:
        """The medium refuses the send; the send's error reaches its caller."""
        self.medium.set_exception(ChannelDeliveryError("the medium refused the send", retryable=False))
        try:
            await self.task
        except ChannelDeliveryError:
            return
        raise AssertionError("a refused send raised nothing")

    async def time_out(self) -> None:
        """The medium never answers; the send passes its bound and its outcome is unknown."""
        try:
            await self.task
        except ChannelDeliveryError:
            return
        raise AssertionError("a send past its bound raised nothing")


@dataclass
class World:
    """Every store a person erase reaches, and the handles a test seeds them through."""

    lua_env: LuaEnv
    states_pg: FakeStatesPg
    parks: tuple[Any, Any]
    saver: RecordingSaver
    channel: FakeChannel = field(default_factory=FakeChannel)
    _clock: float = field(default_factory=time.time)

    @property
    def settings(self):
        return self.lua_env.store.settings

    @property
    def redis(self):
        return self.lua_env.redis

    @property
    def persons(self):
        return self.lua_env.manager.persons

    async def routes(self, *routes: ConversationRoute) -> None:
        """Store ``routes`` and make the target multichannel."""
        for route in routes:
            await self.lua_env.manager.put_route(route)
        await self.lua_env.manager.target_configs.upsert(
            TargetConversationConfig(target_kind=TARGET.target_kind, target_name=TARGET.target_name, multichannel=True)
        )

    async def seed(self, who: Person, *, state: bool = True) -> Person:
        """Plant ``who``'s row and index fields, with a ``person``-subject state unless ``state`` is false."""
        await self.lua_env.seed_person(who)
        if state:
            self.states_pg.seed_record(STATE, *_TARGET_KEY, "person", who.person_id, {"n": 1})
        return who

    def thread_state(self, thread_id: str) -> None:
        """A ``thread``-subject state on ``thread_id``."""
        self.states_pg.seed_record(STATE, *_TARGET_KEY, "thread", thread_id, {"n": 1})

    async def record(
        self, message_id: str, route: ConversationRoute, thread_id: str, address: str, **update: Any
    ) -> str:
        """An answered inbound with its reply in ``thread_id`` from ``address``, created as the door does."""
        self._clock += 1
        record = ConversationRecord(
            message_id=message_id,
            route_name=route.route_name,
            door=route.door,
            thread_id=thread_id,
            client_address=address,
            channel=route.channel,
            our_identity=route.our_identity,
            origin="client",
            inbound_text="hi",
            answer_status="answered",
            answer="ok",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=self._clock,
            updated_at=self._clock,
        )
        return await create_record(self.lua_env.store, record.model_copy(update=update), route=route)

    def install_channel(self, monkeypatch, channel: FakeChannel | None = None) -> FakeChannel:
        """Answer every ``notify_user`` channel lookup with ``channel`` (the world's own by default)."""
        chosen = channel if channel is not None else self.channel
        monkeypatch.setattr(notify_module, "resolve_channel", lambda name: chosen)
        return chosen

    async def notify(self, address: str) -> str:
        """A ``notify_user`` send to the twilio ``address``; answers the send record's id."""
        before = await self.record_ids()
        await notify_user("hello", channel="twilio", recipient=address)
        [message_id] = await self.record_ids() - before
        return message_id

    async def ask(self, address: str, interaction_id: str, our_identity: str = "+15550001111") -> str:
        """An ``ask`` send record to the twilio ``address`` that the medium delivered; answers its id."""
        before = await self.record_ids()
        attempts = SendAttempts()

        async def _send() -> SendOutcome:
            attempts.start()
            return SendOutcome(our_identity=our_identity, address=address, message_ids=[f"ask-{interaction_id}"])

        await recorded_send(
            channel="twilio",
            address=ChannelAddress(our_identity=our_identity, address=address),
            parts=[AnswerPart(message="proceed?")],
            sent_by="ask",
            send=_send,
            bound=30.0,
            ask=AskSend(interaction_id=interaction_id, answer_format="text", sensitive=False),
            attempts=attempts,
        )
        [message_id] = await self.record_ids() - before
        return message_id

    async def held_send(self, address: str, our_identity: str = "+15550001111", *, bound: float = 30.0) -> HeldSend:
        """Begin a ``notify`` send to the twilio ``address`` and hold it inside its medium call."""
        before = await self.record_ids()
        medium: asyncio.Future[SendOutcome] = asyncio.get_running_loop().create_future()
        entered = asyncio.Event()

        async def _send() -> SendOutcome:
            entered.set()
            return await medium

        task = asyncio.ensure_future(
            recorded_send(
                channel="twilio",
                address=ChannelAddress(our_identity=our_identity, address=address),
                parts=[AnswerPart(message="hello")],
                sent_by="notify",
                send=_send,
                bound=bound,
            )
        )
        await asyncio.wait_for(entered.wait(), 10)
        [message_id] = await self.record_ids() - before
        return HeldSend(message_id=message_id, task=task, medium=medium)

    async def park(self, interaction_id: str, thread_id: str, person_id: str | None = None) -> None:
        """An async park bound to ``thread_id``, and to the ``person`` subject ``person_id`` when given."""
        store, fake = self.parks
        await store.add(
            fake,
            _park_request(store, interaction_id, person_id),
            idle_ttl=86400,
            continuation_fingerprint="fp-1",
            thread_id=thread_id,
        )

    async def park_stands(self, interaction_id: str) -> bool:
        store, fake = self.parks
        return await store.get_state(fake, interaction_id) is not None

    async def link(self, person_id_a: str, person_id_b: str) -> Person:
        """The redeem's store steps: the merge, then the re-key of the absorbed person's parks."""
        survivor = await self.persons.merge(person_id_a, person_id_b)
        for absorbed_id in {person_id_a, person_id_b} - {survivor.person_id}:
            await rekey_park_subjects(
                [
                    ("person", absorbed_id, survivor.person_id),
                    ("thread", person_thread_id(absorbed_id), person_thread_id(survivor.person_id)),
                ]
            )
        return survivor

    async def record_ids(self) -> set[str]:
        prefix = self.settings.record_key("")
        return {key.removeprefix(prefix) async for key in self.redis.scan_iter(match=f"{prefix}*")}

    async def dump(self) -> dict[str, Any]:
        """Every conversations key with its whole value."""
        out: dict[str, Any] = {}
        async for key in self.redis.scan_iter(match="*"):
            kind = await self.redis.type(key)
            if kind == "string":
                out[key] = await self.redis.get(key)
            elif kind == "hash":
                out[key] = await self.redis.hgetall(key)
            elif kind == "set":
                out[key] = sorted(await self.redis.smembers(key))
            elif kind == "zset":
                out[key] = await self.redis.zrange(key, 0, -1)
            elif kind == "list":
                out[key] = await self.redis.lrange(key, 0, -1)
            else:
                out[key] = kind
        return out

    async def naming(self, *tokens: str) -> dict[str, Any]:
        """Every conversations key whose name or value carries one of ``tokens``.

        An inbound-dedupe marker under its TTL outlives an erase by design and is left out: a
        provider redelivery must not re-enter the message."""
        dedupe = self.settings.dedupe_key("channel", "id").removesuffix("channel:id")
        found = {}
        for key, value in (await self.dump()).items():
            if not any(t in f"{key} {value}" for t in tokens):
                continue
            if key.startswith(dedupe) and await self.redis.ttl(key) > 0:
                continue
            found[key] = value
        return found

    def states_naming(self, *keys: str) -> list[tuple[str, ...]]:
        """Every states row whose subject key is one of ``keys``."""
        return [row for row in self.states_pg.records if row[4] in keys]

    def interaction_keys_naming(self, *tokens: str) -> list[str]:
        """Every park index set of the interactions store whose name or members carry one of ``tokens``.

        A set with no members is absent, as Redis drops a set when its last member goes. The
        ``subject-scopes`` index is coarse by design: it holds only scope names and may name a scope
        whose parks set is drained, so the parks sets it points at are what is scanned."""
        _store, fake = self.parks
        return [
            f"{key} {sorted(members)}"
            for key, members in fake._sets.items()
            if members and "parks" in key and any(t in f"{key} {sorted(members)}" for t in tokens)
        ]

    def index_field(self, address: PersonAddress) -> str:
        return _address_key_of(address)

    async def index(self) -> dict[str, str]:
        return await self.redis.hgetall(self.settings.person_index_key(*_TARGET_KEY))


async def leftovers(
    world: World,
    *tokens: str,
    threads: tuple[str, ...] = (),
    parks: tuple[str, ...] = (),
    standing: tuple[str, ...] = (),
) -> dict[str, Any]:
    """What still names ``tokens`` or ``threads``: conversations keys, park index keys, states rows,
    standing parks, and threads whose checkpoint was not forgotten.

    ``standing`` are message ids whose records stand elsewhere by design; their rows and the keys
    naming them are not counted."""
    every = (*tokens, *threads)
    keys = {
        key: value
        for key, value in (await world.naming(*every)).items()
        if not any(message_id in f"{key} {value}" for message_id in standing)
    }
    found: dict[str, Any] = {
        "keys": keys,
        "park_index": world.interaction_keys_naming(*every),
        "states": world.states_naming(*tokens, *threads, *(person_thread_id(t) for t in tokens)),
        "parks": [interaction_id for interaction_id in parks if await world.park_stands(interaction_id)],
        "checkpoints": [thread for thread in threads if thread not in world.saver.deleted],
    }
    return {name: value for name, value in found.items() if value}


async def assert_clean(
    world: World,
    *tokens: str,
    threads: tuple[str, ...] = (),
    parks: tuple[str, ...] = (),
    standing: tuple[str, ...] = (),
) -> None:
    """Nothing in any store names ``tokens`` or ``threads``, each park is gone and each checkpoint forgotten."""
    found = await leftovers(world, *tokens, threads=threads, parks=parks, standing=standing)
    assert found == {}, found


__all__ = [
    "CHAT",
    "CHAT_2",
    "LINE",
    "LINE_B",
    "STATE",
    "TARGET",
    "HeldSend",
    "World",
    "api_address",
    "assert_clean",
    "channel_address",
    "leftovers",
    "person",
    "person_thread_id",
    "route_thread_id",
    "state_row",
]
