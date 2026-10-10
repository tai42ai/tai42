"""A chain-delivery fire is authorized by the chain lineage the platform recorded on the park it drives.

A run that parks inside chained nested dispatches records the chain keys of every chained call it is
nested under (outermost first). Whenever the platform drives that interaction — the answer door's
detached drive, the reaper's redelivery, the inline visit under a fresh run frame, the give-up, the
kill from the door and from the reaper — it deposits that lineage beside the resume origin, and
``assert_resume_authorized(chain_token)`` admits a token of that lineage at any depth and refuses a
token of another lineage, as well as any token named outside a platform drive.

A neutral test-only driver takes part: its continuation tool and its kill / give-up handlers fire a
test-only chain face, which asks the platform predicate and records the verdict. No real driver's
code runs.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    ChainedResume,
    InteractionResponse,
    InteractionState,
    ParkResumeUnauthorizedError,
    ResumeItem,
    RunFailed,
    SuspendedInteraction,
    register_park_kill_handler,
    reset_chained_resume,
    reset_resume_continuation_tool,
    set_chained_resume,
    set_resume_continuation_tool,
)
from tai42_contract.interactions import continuation as contract_continuation
from tai42_contract.tools import RunDelivery, run_delivery, tool_call_frame
from tai42_kit.interactions import park_giveup
from tai42_kit.interactions.park_giveup import ParkGiveUpOutcome, register_park_giveup_handler

from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.interactions import ask, authorization, giveup_delivery
from tai42_skeleton.interactions import continuation as continuation_module
from tai42_skeleton.interactions import visit as visit_module
from tai42_skeleton.interactions.kill import kill_park, redeliver_kill
from tai42_skeleton.interactions.store import ContinuationDue, KillDue
from tai42_skeleton.runs.chokepoint import get_resume_lineage, resume_lineage, resume_origin

from ._continuation_support import configure_interactions_store, make_wired

OUTER = "tai42:chained-park:outer"
INNER = "tai42:chained-park:inner"
NEWER = "tai42:chained-park:newer"
FOREIGN = "tai42:chained-park:foreign"
FACE = "probe_chain_face"
RESUME = "probe_resume"


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_interactions_store(monkeypatch)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, fake_redis: Any, fake_client_ctx: Any) -> SimpleNamespace:
    w = make_wired(monkeypatch, fake_redis, fake_client_ctx)
    monkeypatch.setattr(visit_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(visit_module, "interactions_settings", lambda: w.settings)
    # The predicate's store read (an interaction id's stored run identity) resolves against the same fake.
    monkeypatch.setattr(authorization, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(authorization, "interactions_settings", lambda: w.settings)
    monkeypatch.setattr(authorization, "interactions_store_configured", lambda: True)
    monkeypatch.setattr(authorization, "InteractionStore", lambda _prefix: w.store)

    @asynccontextmanager
    async def _bind(identity: str, *, bound_fingerprint: str = ""):
        token = set_execution_identity(CallerIdentity(user_id=identity, execution_key_fingerprint=bound_fingerprint))
        try:
            yield
        finally:
            reset_execution_identity(token)

    monkeypatch.setattr(continuation_module, "bind_execution_identity", _bind)
    return w


@pytest.fixture(autouse=True)
def _isolated_handlers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Both seams are process-global registries: give each test its own empty list.
    monkeypatch.setattr(contract_continuation, "_park_kill_handlers", [])
    saved = list(park_giveup._park_giveup_handlers)
    park_giveup._park_giveup_handlers.clear()
    yield
    park_giveup._park_giveup_handlers[:] = saved


class _ProbeDriver:
    """A neutral driver: its resume, kill and give-up fire its chain face; the face records the verdict.

    ``fire`` is the list of chain tokens the next platform drive fires at the face. ``repark`` (when
    set) makes the next resume ask again, under the routing it names, instead of firing.
    """

    def __init__(self) -> None:
        self.fire: Sequence[str] = ()
        self.repark: ChainedResume | None = None
        self.verdicts: list[tuple[str, str]] = []
        self.reparked: list[SuspendedInteraction] = []

    async def run_tool(
        self, key: str, arguments: Mapping[str, Any], *, offload_sync: bool = False, continues_chain: Any = None
    ) -> Any:
        handler = {RESUME: self._resume, FACE: self._face}.get(key)
        if handler is None:
            return None
        result = handler(arguments)
        return await result if inspect.isawaitable(result) else result

    async def _face(self, arguments: Mapping[str, Any]) -> None:
        if arguments.get("status") == "reparked":
            return
        token = arguments["chain_token"]
        try:
            await authorization.assert_resume_authorized(token)
        except ParkResumeUnauthorizedError:
            self.verdicts.append((token, "refused"))
            return
        self.verdicts.append((token, "admitted"))

    async def fire_all(self) -> None:
        for token in self.fire:
            await tai42_app.tools.run_tool(FACE, {"chain_token": token, "status": "succeeded"})

    async def _resume(self, arguments: Mapping[str, Any]) -> Any:
        if self.repark is not None:
            routing, self.repark = self.repark, None
            tool_token = set_resume_continuation_tool(RESUME)
            token = set_chained_resume(routing)
            try:
                parked = await _ask()
            finally:
                reset_chained_resume(token)
                reset_resume_continuation_tool(tool_token)
            self.reparked.append(parked)
            return parked
        await self.fire_all()
        return {"done": arguments["interaction_id"]}

    async def kill(self, interaction_id: str, reason: str) -> None:
        await self.fire_all()

    async def give_up(self, interaction_id: str, failed_outcome: Mapping[str, Any]) -> ParkGiveUpOutcome:
        await self.fire_all()
        return ParkGiveUpOutcome(RunFailed(outcome=dict(failed_outcome)))


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> _ProbeDriver:
    driver = _ProbeDriver()
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=driver))
    return driver


def _routing(key: str) -> ChainedResume:
    return ChainedResume(delivery_tool=FACE, chain_key=key, asked_by=("holder",))


@contextmanager
def _chained(keys: Sequence[str]) -> Iterator[None]:
    """Bind one chained dispatch per key, outermost first — the nesting a run parks inside."""
    with ExitStack() as stack:
        for key in keys:
            token = set_chained_resume(_routing(key))
            stack.callback(reset_chained_resume, token)
        yield


async def _ask() -> SuspendedInteraction:
    result = await ask("proceed?", mode="async", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    assert isinstance(result, SuspendedInteraction)
    return result


async def _park(wired: SimpleNamespace, keys: Sequence[str]) -> InteractionState:
    """Park an ask of the probe driver's run inside chained dispatches of ``keys``; return its stored state."""
    tool_token = set_resume_continuation_tool(RESUME)
    id_token = set_execution_identity(CallerIdentity(user_id="svc-key", execution_key_fingerprint="fp-1"))
    try:
        with tool_call_frame(), _chained(keys):
            parked = await _ask()
    finally:
        reset_execution_identity(id_token)
        reset_resume_continuation_tool(tool_token)
    state = await wired.store.get_state(wired.fake, parked.interaction_id)
    assert state is not None
    return state


async def _claim(wired: SimpleNamespace, state: InteractionState, answer: Any = "yes") -> None:
    due_ttl, first_attempt_ms = continuation_module.continuation_due_timing(wired.settings)
    claimed = await wired.store.record_answer(
        wired.fake,
        InteractionResponse(
            interaction_id=state.request.interaction_id,
            answer=answer,
            answered_by="u-1",
            answered_at=datetime.now(UTC),
        ),
        state.group_id,
        3600,
        continuation_due_ttl=due_ttl,
        continuation_first_attempt_at_ms=first_attempt_ms,
    )
    assert claimed


async def _settle() -> None:
    # Wait for every detached continuation task the door spawned.
    while continuation_module._CONTINUATION_TASKS:
        await asyncio.gather(*list(continuation_module._CONTINUATION_TASKS), return_exceptions=True)


async def _answer_door(wired: SimpleNamespace, state: InteractionState) -> None:
    """The HTTP answer door's path: the atomic claim, then the shared post-claim fire (detached)."""
    await _claim(wired, state)
    await continuation_module.fire_continuation_after_claim(wired.fake, wired.store, state.request, "yes")
    await _settle()


async def _reaper_redelivery(wired: SimpleNamespace, state: InteractionState) -> None:
    """The reaper's path: the claim's due record, claimed for retry and redelivered (detached)."""
    await _claim(wired, state)
    due = await wired.store.claim_continuation_retry(
        wired.fake, state.request.interaction_id, datetime.now(UTC) + timedelta(hours=2), 1000, 60000
    )
    assert isinstance(due, ContinuationDue)
    continuation_module.redeliver_continuation(wired.store, due)
    await _settle()


async def _inline_visit(wired: SimpleNamespace, state: InteractionState) -> None:
    """A conversation turn answering the pending ask inline, under the turn's OWN fresh run identity."""
    with run_delivery(RunDelivery("rd-the-visiting-turn", None)):
        await visit_module._resume_one(
            wired.store,
            None,
            None,
            ResumeItem(id=state.request.interaction_id, payload="yes"),
            True,
            "yes",
        )


async def _give_up(wired: SimpleNamespace, state: InteractionState, probe: _ProbeDriver) -> None:
    register_park_giveup_handler(probe.give_up)
    await giveup_delivery.deliver_park_giveup(wired.store, state.request, fingerprint="fp-1")


async def _kill_door(wired: SimpleNamespace, state: InteractionState, probe: _ProbeDriver) -> None:
    register_park_kill_handler(probe.kill)
    await kill_park(wired.fake, wired.store, state.request.interaction_id, None, reason="cancelled")


async def _kill_reaper(wired: SimpleNamespace, state: InteractionState, probe: _ProbeDriver) -> None:
    """The kill's teardown raises once after firing (the kill-due record stays), and the reaper redelivers it."""
    attempts = {"n": 0}

    async def _flaky(interaction_id: str, reason: str) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("teardown not ready")
        await probe.kill(interaction_id, reason)

    register_park_kill_handler(_flaky)
    with pytest.raises(RuntimeError, match="teardown not ready"):
        await kill_park(wired.fake, wired.store, state.request.interaction_id, None, reason="cancelled")
    due = await wired.store.claim_kill_retry(
        wired.fake, state.request.interaction_id, datetime.now(UTC) + timedelta(hours=2), 1000, 60000
    )
    assert isinstance(due, KillDue)
    await redeliver_kill(wired.store, due)


async def _drive(door: str, wired: SimpleNamespace, state: InteractionState, probe: _ProbeDriver) -> None:
    if door == "answer":
        await _answer_door(wired, state)
    elif door == "reaper":
        await _reaper_redelivery(wired, state)
    elif door == "visit":
        await _inline_visit(wired, state)
    elif door == "giveup":
        await _give_up(wired, state, probe)
    elif door == "kill":
        await _kill_door(wired, state, probe)
    else:
        await _kill_reaper(wired, state, probe)


DOORS = ["answer", "reaper", "visit", "giveup", "kill", "kill-reaper"]


# --- a token of the driven park's lineage is admitted, at depth 1 and depth 2, at every door ---------


@pytest.mark.parametrize("door", DOORS)
async def test_a_park_one_chained_call_deep_admits_its_holders_token(wired, probe, door) -> None:
    state = await _park(wired, [OUTER])
    probe.fire = [OUTER]
    await _drive(door, wired, state, probe)
    assert probe.verdicts == [(OUTER, "admitted")]


@pytest.mark.parametrize("door", DOORS)
async def test_a_park_two_chained_calls_deep_admits_both_tokens_of_its_lineage(wired, probe, door) -> None:
    # The innermost token is the one the resumed run fires at its holder; the outer token is the one
    # that re-entered holder fires at ITS holder, still inside the platform's drive of this park.
    state = await _park(wired, [OUTER, INNER])
    probe.fire = [INNER, OUTER]
    await _drive(door, wired, state, probe)
    assert probe.verdicts == [(INNER, "admitted"), (OUTER, "admitted")]


@pytest.mark.parametrize("door", DOORS)
async def test_a_token_of_another_lineage_is_refused_inside_the_drive(wired, probe, door) -> None:
    state = await _park(wired, [OUTER, INNER])
    probe.fire = [FOREIGN]
    await _drive(door, wired, state, probe)
    assert probe.verdicts == [(FOREIGN, "refused")]


async def test_a_park_under_no_chained_call_admits_no_chain_token(wired, probe) -> None:
    state = await _park(wired, [])
    probe.fire = [OUTER]
    await _drive("answer", wired, state, probe)
    assert probe.verdicts == [(OUTER, "refused")]


async def test_a_lineage_token_named_outside_any_platform_drive_is_refused(wired, probe) -> None:
    await _park(wired, [OUTER, INNER])
    with pytest.raises(ParkResumeUnauthorizedError, match="no platform resume drive"):
        await authorization.assert_resume_authorized(INNER)


# --- a re-park inside a drive inherits the lineage of the park being driven -------------------------


async def test_a_re_park_under_the_rebound_innermost_routing_inherits_the_lineage(wired, probe) -> None:
    state = await _park(wired, [OUTER, INNER])
    # The resumed run rebinds the routing it captured (the innermost key) and asks again.
    probe.repark = _routing(INNER)
    await _drive("answer", wired, state, probe)
    (reparked,) = probe.reparked
    restate = await wired.store.get_state(wired.fake, reparked.interaction_id)
    assert restate is not None
    assert restate.request.chain_keys == [OUTER, INNER]
    # The second answer drives the re-park; the outermost token is still admitted.
    probe.fire = [OUTER]
    await _drive("answer", wired, restate, probe)
    assert probe.verdicts == [(OUTER, "admitted")]


async def test_a_re_park_under_a_new_nested_chained_call_appends_its_key(wired, probe) -> None:
    state = await _park(wired, [OUTER, INNER])
    probe.repark = _routing(NEWER)
    await _drive("visit", wired, state, probe)
    (reparked,) = probe.reparked
    restate = await wired.store.get_state(wired.fake, reparked.interaction_id)
    assert restate is not None
    assert restate.request.chain_keys == [OUTER, INNER, NEWER]


# --- what a park records, and where the lineage rides ---------------------------------------------


@pytest.mark.parametrize(("keys", "recorded"), [([], []), ([OUTER], [OUTER]), ([OUTER, INNER], [OUTER, INNER])])
async def test_a_first_park_records_every_enclosing_chain_key_outermost_first(wired, keys, recorded) -> None:
    state = await _park(wired, keys)
    assert state.request.chain_keys == recorded


async def test_the_lineage_rides_the_due_record_the_kill_target_and_the_kill_due_record(wired, probe) -> None:
    state = await _park(wired, [OUTER, INNER])
    iid = state.request.interaction_id
    target = await wired.store.read_kill_target(wired.fake, iid)
    assert target is not None
    assert target.chain_keys == [OUTER, INNER]

    await _claim(wired, state)
    due = await wired.store.claim_continuation_retry(wired.fake, iid, datetime.now(UTC) + timedelta(hours=2), 1, 1)
    assert isinstance(due, ContinuationDue)
    assert due.chain_keys == [OUTER, INNER]
    # The state hash aged out: the kill target is read off the due record, lineage included.
    await wired.fake.delete(wired.store.state_key(iid))
    from_due = await wired.store.read_kill_target(wired.fake, iid)
    assert from_due is not None
    assert from_due.chain_keys == [OUTER, INNER]

    async def _not_ready(interaction_id: str, reason: str) -> None:
        raise RuntimeError("teardown not ready")

    register_park_kill_handler(_not_ready)
    with pytest.raises(RuntimeError, match="teardown not ready"):
        await kill_park(wired.fake, wired.store, iid, None, reason="cancelled")
    kill_due = await wired.store.claim_kill_retry(wired.fake, iid, datetime.now(UTC) + timedelta(hours=2), 1, 1)
    assert isinstance(kill_due, KillDue)
    assert kill_due.chain_keys == [OUTER, INNER]


async def test_a_park_nested_under_no_chained_call_stores_no_lineage_field(wired) -> None:
    state = await _park(wired, [])
    assert await wired.fake.hget(wired.store.state_key(state.request.interaction_id), "chain_keys") is None
    target = await wired.store.read_kill_target(wired.fake, state.request.interaction_id)
    assert target is not None
    assert target.chain_keys == []


async def test_the_give_up_runs_its_handler_under_the_parks_lineage(wired, probe) -> None:
    state = await _park(wired, [OUTER, INNER])
    seen: list[tuple[str, ...]] = []

    async def _handler(interaction_id: str, failed_outcome: Mapping[str, Any]) -> None:
        seen.append(get_resume_lineage())

    register_park_giveup_handler(_handler)
    await giveup_delivery.deliver_park_giveup(wired.store, state.request, fingerprint="fp-1")
    assert seen == [(OUTER, INNER)]
    assert get_resume_lineage() == ()


# --- the predicate on a chain key -----------------------------------------------------------------


async def test_the_predicate_admits_a_chain_key_only_in_the_deposited_lineage_of_a_drive() -> None:
    with resume_origin("iid-1"), resume_lineage([OUTER, INNER]):
        await authorization.assert_resume_authorized(OUTER)
        await authorization.assert_resume_authorized(INNER)
        with pytest.raises(ParkResumeUnauthorizedError, match="not a chained call the resumed interaction"):
            await authorization.assert_resume_authorized(FOREIGN)
    # A drive of a park nested under no chained call admits no chain key.
    with resume_origin("iid-1"), pytest.raises(ParkResumeUnauthorizedError):
        await authorization.assert_resume_authorized(OUTER)
    # A lineage with no drive on the stack is no authority.
    with resume_lineage([OUTER]), pytest.raises(ParkResumeUnauthorizedError, match="no platform resume drive"):
        await authorization.assert_resume_authorized(OUTER)
