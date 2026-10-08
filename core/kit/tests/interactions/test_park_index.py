"""The park index library on an in-process Redis with Lua: every method, the lease, the codec."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel
from tai42_contract.interactions import ResumeBuffered, RunFailed, SuspendedInteraction

from tai42_kit.interactions import park_index
from tai42_kit.interactions.park_index import (
    ENTRY_TTL_FLOOR_SECONDS,
    RESOLVED_FIELD,
    BarrierNotFoundError,
    DriveInProgressError,
    LeaseLostError,
    ParkIndex,
    ParkIndexCorruptError,
    ParkValueCodecError,
    ResolutionMissingError,
    ResolutionRecord,
    SuperstepAlreadyResolvedError,
    barrier_ttl_seconds,
    decode_value,
    encode_value,
    entry_ttl_seconds,
    superstep_id,
)

from .conftest import REDELIVERY_HORIZON_SECONDS

THREAD = "thread-1"


async def _park(
    index: ParkIndex, members: list[str], *, thread: str = THREAD, fields: dict[str, str] | None = None
) -> str:
    step = superstep_id(members)
    await index.persist(
        thread_id=thread,
        superstep=step,
        entries={m: {"owner": "probe", "member": m} for m in members},
        expected=dict.fromkeys(members),
        barrier_fields=fields,
        entry_ttl=dict.fromkeys(members, 1000),
        barrier_ttl=2000,
    )
    return step


def test_a_namespace_must_be_lower_case_colon_segments(make_index: Any) -> None:
    for bad in ("", "Probe:park", "probe:", ":park", "probe park", "probe::park"):
        with pytest.raises(ValueError, match="namespace"):
            make_index(bad)
    assert make_index("probe:park-2").namespace == "probe:park-2"


def test_superstep_id_ignores_member_order() -> None:
    assert superstep_id(["b", "a"]) == superstep_id(["a", "b"])
    assert superstep_id(["a"]) != superstep_id(["a", "b"])


def test_ttls_floor_and_extend_past_the_deadline() -> None:
    assert entry_ttl_seconds(None) == ENTRY_TTL_FLOOR_SECONDS
    far = datetime.now(UTC) + timedelta(days=60)
    assert entry_ttl_seconds(far) > ENTRY_TTL_FLOOR_SECONDS
    assert barrier_ttl_seconds([None, far, datetime.now(UTC)]) == entry_ttl_seconds(far)
    assert barrier_ttl_seconds([]) == ENTRY_TTL_FLOOR_SECONDS


async def test_persist_writes_entries_barrier_and_live_set_atomically(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a", "b"], fields={"note": "x"})

    entry = await index.read_entry("a")
    assert entry is not None
    assert entry["owner"] == "probe"
    assert index.tombstone_kind(entry) == "live"
    assert 990 <= await fake_redis.ttl(index.entry_key("a")) <= 1000
    barrier = await index.read_barrier(THREAD, step)
    assert barrier is not None
    assert barrier.expected == {"a": None, "b": None}
    assert barrier.outputs == {}
    assert barrier.fields == {"note": "x"}
    assert await fake_redis.smembers(index.live_set_key(THREAD)) == {step}
    assert 1990 <= await fake_redis.ttl(index.live_set_key(THREAD)) <= 2000
    assert await index.read_entry("never") is None
    assert await index.read_barrier(THREAD, "nope") is None


async def test_two_namespaces_on_one_redis_never_collide(make_index: Any) -> None:
    first, second = make_index("probe:park"), make_index("other:park")
    await _park(first, ["a"])
    assert await second.read_entry("a") is None
    assert await second.threads_with_live_barriers([THREAD]) == set()
    assert await first.threads_with_live_barriers([THREAD]) == {THREAD}


async def test_buffer_is_idempotent_and_reports_the_open_members(make_index: Any) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a", "b", "c"])

    first = await index.buffer(THREAD, step, "b", {"answer": 1})
    assert (first.present, first.total, first.remaining) == (1, 3, ["a", "c"])
    again = await index.buffer(THREAD, step, "b", {"answer": "changed"})
    assert (again.present, again.remaining) == (1, ["a", "c"])
    barrier = await index.read_barrier(THREAD, step)
    assert barrier is not None
    assert barrier.outputs == {"b": {"answer": 1}}

    with pytest.raises(KeyError):
        await index.buffer(THREAD, step, "stranger", 1)
    with pytest.raises(BarrierNotFoundError):
        await index.buffer(THREAD, "gone", "a", 1)


async def test_a_buffered_contract_answer_keeps_its_type(make_index: Any) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    await index.buffer(THREAD, step, "a", RunFailed(outcome={"why": "down"}))
    barrier = await index.read_barrier(THREAD, step)
    assert barrier is not None
    assert barrier.outputs == {"a": RunFailed(outcome={"why": "down"})}


async def test_a_buffer_racing_the_finalize_deletes_the_resurrected_key(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a", "b"])
    real_hsetnx = fake_redis.hsetnx

    async def finalize_then_write(key: str, field: str, value: str) -> int:
        await fake_redis.delete(key)
        return await real_hsetnx(key, field, value)

    fake_redis.hsetnx = finalize_then_write
    with pytest.raises(SuperstepAlreadyResolvedError):
        await index.buffer(THREAD, step, "a", 1)
    assert not await fake_redis.exists(index.barrier_key(THREAD, step))


async def test_set_barrier_fields_writes_a_live_barrier_and_never_resurrects(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    assert await index.set_barrier_fields(THREAD, step, {"state": "linked", "link": "[]"}) is True
    barrier = await index.read_barrier(THREAD, step)
    assert barrier is not None
    assert barrier.fields == {"state": "linked", "link": "[]"}

    await fake_redis.delete(index.barrier_key(THREAD, step))
    assert await index.set_barrier_fields(THREAD, step, {"state": "standalone"}) is False
    assert not await fake_redis.exists(index.barrier_key(THREAD, step))
    with pytest.raises(ValueError, match="owned"):
        await index.set_barrier_fields(THREAD, step, {"expected": "{}"})


async def test_claim_is_won_once_and_released_on_exit(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    async with index.claim(THREAD, step) as lease:
        assert await lease.holds()
        with pytest.raises(DriveInProgressError):
            await index.claim(THREAD, step).acquire()
    assert not await fake_redis.exists(index.lease_key(THREAD, step))
    async with index.claim(THREAD, step) as again:
        assert await again.holds()


async def test_closing_a_lease_never_swallows_a_cancel_of_the_closing_task(make_index: Any, fake_redis: Any) -> None:
    """A cancel requested of the task closing the lease propagates out of ``close``; the lease is still released."""
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    lease = await index.claim(THREAD, step).acquire()

    async def close_while_cancelled() -> str:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await lease.close()
        return "close returned"

    closing = asyncio.ensure_future(close_while_cancelled())
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not await fake_redis.exists(index.lease_key(THREAD, step))


async def test_closing_a_lease_after_a_handled_cancel_returns_normally(make_index: Any, fake_redis: Any) -> None:
    """A close run while handling a cancel already delivered stops the heartbeat and releases without raising."""
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    lease = await index.claim(THREAD, step).acquire()

    async def handle_cancel_then_close() -> str:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await lease.close()
            return "closed inside the cancel handler"
        return "never cancelled"

    task = asyncio.ensure_future(handle_cancel_then_close())
    await asyncio.sleep(0)
    task.cancel()
    assert await task == "closed inside the cancel handler"
    assert not await fake_redis.exists(index.lease_key(THREAD, step))


async def test_finalize_tombstones_records_and_clears_under_the_lease(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a", "b"])
    async with index.claim(THREAD, step) as lease:
        await index.finalize(lease, member_ids=["a", "b"], resolution="terminal", value={"done": True})
        assert lease.consumed

    for member in ("a", "b"):
        entry = await index.read_entry(member)
        assert entry is not None
        assert index.tombstone_kind(entry) == "resolved"
        assert index.tombstone_coordinates(entry) == (THREAD, step)
        assert await fake_redis.ttl(index.entry_key(member)) == 2 * REDELIVERY_HORIZON_SECONDS
    assert await index.read_resolution(THREAD, step) == ResolutionRecord("terminal", {"done": True})
    assert await index.read_barrier(THREAD, step) is None
    assert not await fake_redis.exists(index.lease_key(THREAD, step))
    assert await fake_redis.smembers(index.live_set_key(THREAD)) == set()
    assert await index.run_resolutions(THREAD) == {step: ["a", "b"]}
    assert await index.threads_with_live_barriers([THREAD]) == set()


async def test_a_stale_token_cannot_finalize(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    stale = await index.claim(THREAD, step).acquire()
    await fake_redis.set(index.lease_key(THREAD, step), "another-holder")
    with pytest.raises(LeaseLostError):
        await index.finalize(stale, member_ids=["a"], resolution="terminal", value=1)
    entry = await index.read_entry("a")
    assert entry is not None
    assert index.tombstone_kind(entry) == "live"
    await stale.close()
    assert await fake_redis.get(index.lease_key(THREAD, step)) == "another-holder"


async def test_an_unacquired_or_consumed_lease_cannot_finalize(make_index: Any) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    with pytest.raises(LeaseLostError):
        await index.finalize(index.claim(THREAD, step), member_ids=["a"], resolution="terminal", value=1)
    async with index.claim(THREAD, step) as lease:
        await index.finalize(lease, member_ids=["a"], resolution="terminal", value=1)
        with pytest.raises(LeaseLostError):
            await index.finalize(lease, member_ids=["a"], resolution="terminal", value=2)


async def test_finalize_refuses_an_unknown_resolution_and_an_unstorable_value(make_index: Any) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    async with index.claim(THREAD, step) as lease:
        with pytest.raises(ParkIndexCorruptError):
            await index.finalize(lease, member_ids=["a"], resolution="maybe", value=1)  # type: ignore[arg-type]
        with pytest.raises(ParkValueCodecError):
            await index.finalize(lease, member_ids=["a"], resolution="terminal", value=object())
        assert await lease.holds()


async def test_a_finalize_under_a_handed_lease_consumes_it(
    make_index: Any, fake_redis: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(park_index, "DRIVE_LEASE_HEARTBEAT_SECONDS", 0.01)
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    released: list[str] = []
    async with index.claim(THREAD, step) as lease:
        real_release = lease.release

        async def spy_release() -> None:
            released.append("released")
            await real_release()

        lease.release = spy_release  # type: ignore[method-assign]

        async def callee(handed: Any) -> None:
            await index.finalize(handed, member_ids=["a"], resolution="suspended", value={"parked": True})

        await callee(lease)
        await asyncio.sleep(0.05)
        assert lease.consumed
        assert not lease.lost
    assert released == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_the_heartbeat_renews_and_detects_a_stolen_lease(
    make_index: Any, fake_redis: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(park_index, "DRIVE_LEASE_HEARTBEAT_SECONDS", 0.01)
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    key = index.lease_key(THREAD, step)
    async with index.claim(THREAD, step) as lease:
        await fake_redis.expire(key, 5)
        await asyncio.sleep(0.05)
        assert await fake_redis.ttl(key) > 5
        await fake_redis.set(key, "thief")
        await asyncio.sleep(0.05)
        assert lease.lost
        with pytest.raises(LeaseLostError):
            await index.finalize(lease, member_ids=["a"], resolution="terminal", value=1)
    assert any(r.levelno == logging.ERROR and "taken by another writer" in r.getMessage() for r in caplog.records)
    assert await fake_redis.get(key) == "thief"


async def test_a_heartbeat_that_cannot_reach_redis_marks_the_lease_lost(
    make_index: Any, fake_redis: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(park_index, "DRIVE_LEASE_HEARTBEAT_SECONDS", 0.01)
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    lease = await index.claim(THREAD, step).acquire()

    async def down(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(fake_redis, "eval", down)
    await asyncio.sleep(0.05)
    assert lease.lost
    assert any(r.levelno == logging.ERROR and "could not be renewed" in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    await lease.close()


async def test_a_resolved_tombstone_replays_its_record_and_a_missing_record_raises(
    make_index: Any, fake_redis: Any
) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    async with index.claim(THREAD, step) as lease:
        await index.finalize(lease, member_ids=["a"], resolution="aborted", value=RunFailed(outcome={"r": "kill"}))
    entry = await index.read_entry("a")
    assert entry is not None
    assert await index.read_tombstone_resolution(entry) == ResolutionRecord("aborted", RunFailed(outcome={"r": "kill"}))

    await fake_redis.delete(index.resolution_key(THREAD, step))
    with pytest.raises(ResolutionMissingError):
        await index.read_tombstone_resolution(entry)
    live = await _park(index, ["z"])
    live_entry = await index.read_entry("z")
    assert live_entry is not None
    assert live
    with pytest.raises(ParkIndexCorruptError):
        await index.read_tombstone_resolution(live_entry)


async def test_detach_writes_a_record_less_tombstone_and_never_overwrites(make_index: Any) -> None:
    index = make_index("probe:park")
    await _park(index, ["held"])
    await index.detach(["claimed", "held"])
    await index.detach([])

    detached = await index.read_entry("claimed")
    assert detached == {RESOLVED_FIELD: True}
    assert index.tombstone_kind(detached) == "detached"
    assert await index.read_tombstone_resolution(detached) is None
    held = await index.read_entry("held")
    assert held is not None
    assert index.tombstone_kind(held) == "live"


async def test_a_corrupt_record_or_entry_raises(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    await fake_redis.set(index.resolution_key(THREAD, "s"), json.dumps({"resolution": "won", "value": encode_value(1)}))
    with pytest.raises(ParkIndexCorruptError):
        await index.read_resolution(THREAD, "s")
    await fake_redis.set(index.resolution_key(THREAD, "s"), json.dumps({"resolution": "terminal"}))
    with pytest.raises(ParkIndexCorruptError):
        await index.read_resolution(THREAD, "s")
    await fake_redis.set(index.entry_key("x"), "[1]")
    with pytest.raises(ParkIndexCorruptError):
        await index.read_entry("x")
    await fake_redis.hset(index.barrier_key(THREAD, "s"), mapping={"output:a": "1"})
    with pytest.raises(ParkIndexCorruptError):
        await index.read_barrier(THREAD, "s")


async def test_run_resolutions_and_their_drops(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    first = await _park(index, ["a"])
    async with index.claim(THREAD, first) as lease:
        await index.finalize(lease, member_ids=["a"], resolution="suspended", value=1)
    second = await _park(index, ["b", "c"])
    async with index.claim(THREAD, second) as lease:
        await index.finalize(lease, member_ids=["b", "c"], resolution="terminal", value=2)
    assert await index.run_resolutions(THREAD) == {first: ["a"], second: ["b", "c"]}

    await index.drop_resolution(THREAD, first, member_ids=["extra"])
    assert await index.read_entry("a") is None
    assert await index.read_resolution(THREAD, first) is None
    assert await index.run_resolutions(THREAD) == {second: ["b", "c"]}

    await index.drop_run_resolutions(THREAD, keep=second)
    assert await index.read_resolution(THREAD, second) is not None
    assert await index.run_resolutions(THREAD) == {}
    await index.drop_run_resolutions(THREAD)
    assert await index.read_entry("b") is not None


async def test_a_kill_style_teardown_leaves_no_barrier_or_lease(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    earlier = await _park(index, ["old"])
    async with index.claim(THREAD, earlier) as lease:
        await index.finalize(lease, member_ids=["old"], resolution="suspended", value=1)
    step = await _park(index, ["a", "b"])

    async with index.claim(THREAD, step) as lease:
        await index.drop_run_resolutions(THREAD, keep=step)
        await index.finalize(lease, member_ids=["a", "b"], resolution="aborted", value=RunFailed(outcome={"k": 1}))

    assert await index.read_entry("old") is None
    assert await index.read_resolution(THREAD, earlier) is None
    for member in ("a", "b"):
        entry = await index.read_entry(member)
        assert entry is not None
        assert index.tombstone_kind(entry) == "resolved"
    assert not await fake_redis.exists(index.barrier_key(THREAD, step))
    assert not await fake_redis.exists(index.lease_key(THREAD, step))
    assert await index.run_resolutions(THREAD) == {step: ["a", "b"]}


async def test_threads_with_live_barriers(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    await _park(index, ["a"], thread="live")
    expired = await _park(index, ["b"], thread="expired")
    await fake_redis.delete(index.barrier_key("expired", expired))
    done = await _park(index, ["c"], thread="done")
    async with index.claim("done", done) as lease:
        await index.finalize(lease, member_ids=["c"], resolution="terminal", value=None)

    assert await index.threads_with_live_barriers(["live", "expired", "done", "never"]) == {"live"}
    assert await index.threads_with_live_barriers([]) == set()


async def test_extend_horizon_only_grows_a_live_park(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    far = datetime.now(UTC) + timedelta(days=90)

    assert await index.extend_horizon("a", far) is True
    assert await fake_redis.ttl(index.entry_key("a")) > ENTRY_TTL_FLOOR_SECONDS
    assert await fake_redis.ttl(index.barrier_key(THREAD, step)) > ENTRY_TTL_FLOOR_SECONDS
    assert await fake_redis.ttl(index.live_set_key(THREAD)) > ENTRY_TTL_FLOOR_SECONDS
    grown = await fake_redis.ttl(index.entry_key("a"))
    assert await index.extend_horizon("a", datetime.now(UTC)) is True
    assert await fake_redis.ttl(index.entry_key("a")) == grown

    assert await index.extend_horizon("never", far) is False
    async with index.claim(THREAD, step) as lease:
        await index.finalize(lease, member_ids=["a"], resolution="terminal", value=None)
    assert await index.extend_horizon("a", far) is False


async def test_extend_horizon_refuses_an_entry_without_coordinates(make_index: Any, fake_redis: Any) -> None:
    index = make_index("probe:park")
    await fake_redis.set(index.entry_key("odd"), json.dumps({"owner": "someone"}))
    with pytest.raises(ParkIndexCorruptError):
        await index.extend_horizon("odd", None)


class _Foreign(BaseModel):
    x: int = 1


@pytest.mark.parametrize(
    "value",
    [
        SuspendedInteraction(interaction_id="i-1"),
        ResumeBuffered(remaining_ids=["i-2"]),
        RunFailed(outcome={"status": "error"}),
        {"plain": [1, "two", None]},
        None,
        "text",
    ],
)
def test_the_codec_round_trips_every_tag(value: Any) -> None:
    decoded = decode_value(json.loads(json.dumps(encode_value(value))))
    assert decoded == value
    assert type(decoded) is type(value)


def test_the_codec_refuses_a_foreign_model_an_unserializable_value_and_an_unknown_tag() -> None:
    with pytest.raises(ParkValueCodecError, match="_Foreign"):
        encode_value(_Foreign())
    with pytest.raises(ParkValueCodecError):
        encode_value({"when": datetime.now(UTC)})
    for bad in ({"__contract__": "mystery", "data": 1}, {"data": 1}, "raw", {"__contract__": "value"}):
        with pytest.raises(ParkValueCodecError, match="unknown park value tag"):
            decode_value(bad)


def test_a_suspended_value_never_reads_as_a_plain_dict() -> None:
    encoded = encode_value(SuspendedInteraction(interaction_id="i-1"))
    assert encoded["__contract__"] == "suspended_interaction"
    assert isinstance(decode_value(encoded), SuspendedInteraction)


def test_importing_the_interactions_package_or_the_library_loads_no_redis() -> None:
    probe = (
        "import sys\n"
        "import tai42_kit.interactions\n"
        "import tai42_kit.interactions.park_index\n"
        "import tai42_kit.interactions.park_adoption\n"
        "print(sorted(m for m in sys.modules if m == 'redis' or m.startswith('redis.')))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True).stdout.strip()
    assert out == "[]"


async def test_the_index_refuses_misuse_loudly(make_index: Any) -> None:
    index, other = make_index("probe:park"), make_index("other:park")
    step = await _park(index, ["a"])
    with pytest.raises(ValueError, match="at least one field"):
        await index.set_barrier_fields(THREAD, step, {})
    async with other.claim(THREAD, step) as foreign:
        with pytest.raises(ValueError, match="claimed on this park index"):
            await index.finalize(foreign, member_ids=["a"], resolution="terminal", value=1)
    lease = index.claim(THREAD, step)
    assert await lease.holds() is False
    await lease.acquire()
    with pytest.raises(RuntimeError, match="acquired once"):
        await lease.acquire()
    await lease.close()
    await lease.close()
    assert not lease.consumed


async def test_a_consumed_lease_stops_its_heartbeat_before_a_renew(
    make_index: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat waking after the finalize consumed its lease stops without renewing anything."""
    index = make_index("probe:park")
    step = await _park(index, ["a"])
    lease = await index.claim(THREAD, step).acquire()
    lease.consumed = True
    monkeypatch.setattr(park_index, "DRIVE_LEASE_HEARTBEAT_SECONDS", 0.0)
    await lease._beat()
    assert not lease.lost
    await lease.close()


async def test_the_default_client_is_the_kits_pooled_redis_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no client factory, the index reaches Redis through the kit's pooled client for its settings."""
    import contextlib

    from tai42_kit import clients
    from tai42_kit.clients.settings import RedisConnectionSettings

    seen: list[Any] = []

    @contextlib.asynccontextmanager
    async def _client_ctx(client_cls: Any, settings: Any) -> AsyncIterator[Any]:
        seen.append((client_cls.__name__, settings.redis_url))
        yield "pooled-client"

    monkeypatch.setattr(clients, "client_ctx", _client_ctx)
    index = ParkIndex("probe:park", RedisConnectionSettings(redis_url="redis://probe"))
    async with index._client() as client:
        assert client == "pooled-client"
    assert seen == [("RedisClient", "redis://probe")]
