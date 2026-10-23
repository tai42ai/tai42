"""Shared wiring for the hook-run tests.

* ``clock`` freezes the hook-run clock (``runtime.now``), so leases, due times and backoff waits
  are exact.
* ``backend`` runs a test against both hook-run stores: the in-memory manager, and the Redis
  manager over a Lua-executing fake Redis. ``backend.peer(manager)`` is a second manager on the
  same store (on Redis a second instance; in memory the same one), and ``as_process`` makes this
  process claim under another claimer id, as a second process would.
* ``world`` binds a fake ``tai42_app`` whose tools, ``$parked`` read and visit a test scripts.
* ``notices`` records the operator notifications the loud path writes.
* ``tool_runs_store`` turns the tool-run store on over an in-process fake.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from prometheus_client import REGISTRY
from tai42_contract.app import tai42_app
from tai42_contract.hooks import HookParams, HookSubject
from tai42_contract.interactions import ParkedEntry, VisitAct, VisitOutcome
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_contract.template import TemplatedText
from tai42_contract.tools import (
    TOOL_META_CRASH_RESUME,
    ToolInvocation,
    current_tool_invocation,
    reset_current_tool_invocation,
    set_current_tool_invocation,
)
from tai42_kit.utils.state_context import current_state_context

from tai42_skeleton.authz.execution_identity import get_execution_identity
from tai42_skeleton.hooks.managers.base_hooks_manager import BaseHooksManager
from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.managers.redis_hooks_manager import RedisHooksManager
from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs.models import HookRun
from tai42_skeleton.hooks.settings import HooksSettings

from ..._fakes.interactions_redis import FakeRedis as InteractionsFakeRedis
from ..._fakes.tool_runs_redis import FakeRedis as ToolRunsFakeRedis

START = 1_800_000_000.0


class Clock:
    """A settable stand-in for ``runtime.now``."""

    def __init__(self, start: float) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    frozen = Clock(START)
    monkeypatch.setattr(runtime, "now", frozen)
    return frozen


def as_process(monkeypatch: pytest.MonkeyPatch, claimer: str) -> None:
    """Make this process claim, from now on, under ``claimer``."""
    monkeypatch.setattr(runtime, "_CLAIMED_BY", claimer)


# -- the two stores -----------------------------------------------------------------


class Backend:
    """Builds hook managers on one store: in memory, or Redis over one Lua-executing fake."""

    def __init__(self, kind: str, redis: aioredis.FakeRedis | None) -> None:
        self.kind = kind
        self.redis = redis

    def manager(self, **settings: Any) -> BaseHooksManager:
        hooks_settings = HooksSettings(**settings)
        if self.kind == "memory":
            return InMemoryHooksManager(hooks_settings)
        return RedisHooksManager(hooks_settings)

    def peer(self, manager: BaseHooksManager) -> BaseHooksManager:
        """A second manager on ``manager``'s store."""
        if self.kind == "memory":
            return manager
        return RedisHooksManager(manager.settings)


@pytest.fixture(params=["memory", "redis"])
async def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Backend]:
    if request.param == "memory":
        yield Backend("memory", None)
        return
    from tai42_skeleton.hooks.managers import redis_hooks_manager as redis_module

    client = aioredis.FakeRedis(decode_responses=True)

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield client

    monkeypatch.setattr(redis_module, "client_ctx", _ctx)
    try:
        yield Backend("redis", client)
    finally:
        await client.aclose()


@pytest.fixture
def memory_backend() -> Backend:
    return Backend("memory", None)


async def get_run(manager: BaseHooksManager, run_id: str) -> HookRun | None:
    """The stored job ``run_id``, or ``None`` when it is gone."""
    page = await manager.list_hook_runs(None, 200, None)
    for run in page.items:
        if run.id == run_id:
            return run
    return None


async def held_run(manager: BaseHooksManager, run_id: str) -> HookRun:
    """The stored job ``run_id``; fails the test when it is gone."""
    run = await get_run(manager, run_id)
    assert run is not None, f"hook run {run_id} is not in the store"
    return run


async def all_runs(manager: BaseHooksManager) -> list[HookRun]:
    return list((await manager.list_hook_runs(None, 200, None)).items)


def hook(
    name: str = "h",
    topic: str = "t",
    tool: str = "probe",
    **fields: Any,
) -> HookParams:
    return HookParams(
        name=name,
        topic=topic,
        tool=tool,
        execution_key=fields.pop("execution_key", "k-hook"),
        execution_key_fingerprint=fields.pop("execution_key_fingerprint", "fp-hook"),
        **fields,
    )


def metric(name: str, **labels: str) -> float:
    """A Prometheus sample's value, ``0`` before it is first counted."""
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


async def settle_loop(turns: int = 20) -> None:
    """Let the spawned tasks run up to their next real wait."""
    for _ in range(turns):
        await asyncio.sleep(0)


# -- the fake app -------------------------------------------------------------------


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    extras: dict[str, Any]
    state_context: StateContext | None
    invocation: ToolInvocation | None
    user_id: str | None


Behaviour = Callable[[ToolCall], Awaitable[Any]]


class FakeTools:
    """A tool runner recording every call with the ambient facts it ran under.

    ``behave[name]`` scripts a tool: an awaitable taking the call, whose return is the tool's
    result. ``flagged`` names the tools that declare crash-resume.
    """

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []
        self.behave: dict[str, Behaviour] = {}
        self.flagged: set[str] = set()

    def of(self, name: str) -> list[ToolCall]:
        return [call for call in self.calls if call.name == name]

    async def get_tool(self, name: str) -> Any:
        return SimpleNamespace(name=name, meta={TOOL_META_CRASH_RESUME: True} if name in self.flagged else None)

    async def run_tool(self, name, tool_input, *, offload_sync=False, extras=None):
        identity = get_execution_identity()
        call = ToolCall(
            name=name,
            input=dict(tool_input),
            extras=dict(extras or {}),
            state_context=current_state_context(),
            invocation=current_tool_invocation(),
            user_id=None if identity is None else identity.user_id,
        )
        self.calls.append(call)
        behaviour = self.behave.get(name)
        if behaviour is not None:
            return await behaviour(call)
        return {"ok": True}


def fail_times(times: int, make: Callable[[], BaseException]) -> Behaviour:
    """A tool that raises ``make()`` on its first ``times`` calls, then succeeds."""
    seen = {"n": 0}

    async def _run(call: ToolCall) -> Any:
        seen["n"] += 1
        if seen["n"] <= times:
            raise make()
        return {"ok": True}

    return _run


def blocked(gate: asyncio.Event, started: asyncio.Event | None = None) -> Behaviour:
    """A tool that blocks until ``gate`` is set; ``started`` is set when it begins."""

    async def _run(call: ToolCall) -> Any:
        if started is not None:
            started.set()
        await gate.wait()
        return {"ok": True}

    return _run


class _InlineResources:
    async def render_templated_text(self, text, locale=None):
        return text.content


@dataclass
class FakeInteractions:
    """The interactions facade a hook drives through, scripted by the test.

    ``list_parked_for`` raises the queued ``parked_faults`` first (one per read), waits on
    ``parked_gate`` when one is set, then answers ``parked``. ``visit`` notifies ``on_act`` before
    the cancels and before the start, as the real visit does, and drives the start; it drives no
    resume or take. ``settle_visit_claim`` answers the ids listed in ``gone``.
    """

    parked: list[ParkedEntry] = field(default_factory=list)
    parked_faults: list[BaseException] = field(default_factory=list)
    parked_gate: asyncio.Event | None = None
    parked_started: asyncio.Event = field(default_factory=asyncio.Event)
    parked_reads: list[StateContext | None] = field(default_factory=list)
    visit_calls: list[SimpleNamespace] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    on_cancel: Callable[[list[str]], Awaitable[None]] | None = None
    gone: set[str] = field(default_factory=set)
    settle_calls: list[VisitAct] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    async def list_parked_for(self, context):
        self.order.append("list_parked_for")
        self.parked_reads.append(context)
        self.parked_started.set()
        if self.parked_gate is not None:
            await self.parked_gate.wait()
        if self.parked_faults:
            raise self.parked_faults.pop(0)
        return list(self.parked)

    async def settle_visit_claim(self, act):
        self.order.append("settle")
        self.settle_calls.append(act)
        return tuple(i for i in act.interaction_ids if i in self.gone)

    async def visit(
        self, *, target_name, cancel, resume, start, extras, state_binding=None, receives_outcome=True, on_act=None
    ):
        self.order.append("visit")
        self.visit_calls.append(
            SimpleNamespace(
                target_name=target_name,
                cancel=list(cancel),
                resume=list(resume),
                extras=dict(extras),
                state_binding=state_binding,
                receives_outcome=receives_outcome,
                started=start is not None,
            )
        )
        if resume:
            raise AssertionError("the fake visit drives no resume or take; use the real interactions wiring")
        if cancel:
            if on_act is not None:
                await on_act(VisitAct(action="cancel", interaction_ids=tuple(cancel), phase="before"))
            if self.on_cancel is not None:
                await self.on_cancel(list(cancel))
            self.cancelled.extend(cancel)
        result = None
        if start is not None:
            if on_act is not None:
                await on_act(VisitAct(action="start", interaction_ids=(), phase="before"))
            token = (
                set_current_tool_invocation(ToolInvocation(tool_name=target_name, state_binding=state_binding))
                if state_binding is not None
                else None
            )
            try:
                result = await start(extras)
            finally:
                if token is not None:
                    reset_current_tool_invocation(token)
        return VisitOutcome(
            action="started" if start is not None else "none",
            kind="result" if start is not None else "none",
            result=result,
        )

    async def normalise_started(self, value):
        if value is None:
            return VisitOutcome(action="started", kind="none")
        return VisitOutcome(action="started", kind="result", result=value)


class FakeApp:
    def __init__(self) -> None:
        self.tools = FakeTools()
        self.storage = SimpleNamespace(resource_manager=_InlineResources())
        self.interactions = FakeInteractions()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> FakeApp:
    """Bind a fake ``tai42_app`` for the test, with the tool-run store off."""
    monkeypatch.delenv("TAI_TOOL_RUNS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    app = FakeApp()
    monkeypatch.setattr(tai42_app, "_impl", app)
    return app


@pytest.fixture(autouse=True)
def notices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The operator notifications the loud path writes, with a configured feed."""
    from tai42_skeleton.channels import notifications_sink
    from tai42_skeleton.interactions import settings as interactions_settings_module

    written: list[str] = []

    async def _record(message, *args, **kwargs):
        written.append(message)

    monkeypatch.setattr(notifications_sink, "record_notification", _record)
    monkeypatch.setattr(interactions_settings_module, "interactions_store_configured", lambda: True)
    return written


@pytest.fixture
def tool_runs_store(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Turn the tool-run store on over an in-process fake; yields the fake, its store and the module."""
    ops = importlib.import_module("tai42_skeleton.operations.tool_runs")
    from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings

    monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")
    fake = ToolRunsFakeRedis()
    settings = ToolRunsSettings()

    @asynccontextmanager
    async def ctx(client_cls, s=None, *, fresh=False, **kwargs):
        yield fake

    monkeypatch.setattr(ops, "client_ctx", ctx)
    monkeypatch.setattr(ops, "tool_runs_settings", lambda: settings)
    monkeypatch.setattr(ops, "_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    return SimpleNamespace(fake=fake, store=ops.ToolRunStore(settings.key_prefix), ops=ops)


# -- the real interactions wiring ---------------------------------------------------

RESUME_TOOL_MODULE = "tests.app._fixtures.resume_tool"
RETRY_TOOL_MODULE = "tests.hooks.runs._retry_tool"


def park_candidates() -> SubjectCandidates:
    """The subject every seeded park sits on, and the one ``real_hook`` resolves to."""
    return SubjectCandidates(target_kind="agent", target_name="a", by_kind={"person": "pA"})


def real_hook(name: str = "h", topic: str = "t", **fields: Any) -> HookParams:
    """A hook on the seeded parks' subject (payload ``who``) that starts nothing unless told to."""
    fields.setdefault("start_expr", TemplatedText(content="null"))
    return hook(
        name,
        topic,
        subject=HookSubject(
            target_kind="agent", target_name="a", kind="person", key_expr=TemplatedText(content=".who")
        ),
        **fields,
    )


class _RealApp:
    """The process app, with templated text rendered inline."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self.storage = SimpleNamespace(resource_manager=_InlineResources())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._app, name)


@dataclass
class RealWorld:
    """The real visit, continuation and ``$parked`` read over one fake interactions Redis."""

    redis: Any
    store: Any
    settings: Any
    reads: dict[str, Any]

    async def park(self, iid: str, *, gid: str = "g1", to: str = "caller") -> None:
        """Seed an async caller ask ``iid`` whose continuation is ``resume_tool``."""
        from tai42_contract.interactions import AnswerFormat, InteractionRequest

        now = datetime.now(UTC)
        expiry = now + timedelta(minutes=60)
        request = InteractionRequest(
            interaction_id=iid,
            group_id=gid,
            question="proceed?",
            answer_format=AnswerFormat.FREE,
            reply_to=self.store.reply_key(iid),
            created_at=now,
            timeout_at=expiry,
            mode="async",
            to=to,  # type: ignore[arg-type]
            continuation_tool="resume_tool",
            continuation_identity="svc-key",
            continuation_state_context=StateContext(
                door="conversation", candidates=park_candidates(), actor="u-1", turn_id="t-1"
            ),
            expiry_at=expiry,
            run_delivery_id=f"rd-{iid}",
        )
        await self.store.add(self.redis, request, idle_ttl=86400, to=to)

    async def outcome(self, cid: str, status: str, result: Any) -> None:
        """Seed a waiting outcome ``cid`` a take can claim."""
        await self.store.add_outcome(
            self.redis,
            completion_id=cid,
            interaction_id=cid,
            status=status,  # type: ignore[arg-type]
            result=result,
            candidates=park_candidates(),
            retention_ttl=3600,
        )

    # The reads below call the store class's own methods as the fixture found them, so a test that
    # patches a store method to fault reads the true state around it.

    async def status(self, iid: str) -> str | None:
        state = await self.reads["get_state"](self.store, self.redis, iid)
        return None if state is None else state.status

    async def due_present(self, iid: str) -> bool:
        return bool(await self.redis.hgetall(self.store.continuation_due_key(iid)))

    async def outcome_waiting(self, iid: str) -> bool:
        return await self.reads["outcome_waiting"](self.store, self.redis, iid)


def flaky(
    monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, faults: Sequence[BaseException]
) -> list[tuple[Any, ...]]:
    """Patch ``owner.name`` to raise the queued ``faults`` on its first calls, then run as before; returns the calls."""
    original = getattr(owner, name)
    calls: list[tuple[Any, ...]] = []
    queued = list(faults)

    async def _flaky(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        if queued:
            raise queued.pop(0)
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, name, _flaky)
    return calls


@pytest.fixture
async def real_world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RealWorld]:
    """Bind the process app, with ``resume_tool`` and ``retried_probe`` registered, over a fake
    interactions Redis; tool-run store off."""
    from tai42_skeleton.app import instance
    from tai42_skeleton.interactions import continuation as continuation_module
    from tai42_skeleton.interactions import visit as visit_module
    from tai42_skeleton.interactions.settings import InteractionsSettings
    from tai42_skeleton.interactions.store import InteractionStore
    from tai42_skeleton.manifest import Manifest

    monkeypatch.delenv("TAI_TOOL_RUNS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    settings = InteractionsSettings()
    fake_redis = InteractionsFakeRedis()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, s=None, *, fresh=False, **kwargs):
        yield fake_redis

    monkeypatch.setattr(visit_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(visit_module, "interactions_settings", lambda: settings)
    monkeypatch.setattr(visit_module, "interactions_store_configured", lambda: True)
    monkeypatch.setattr(continuation_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(continuation_module, "interactions_settings", lambda: settings)

    @asynccontextmanager
    async def _fake_bind(identity, *, bound_fingerprint=""):
        yield

    monkeypatch.setattr(continuation_module, "bind_execution_identity", _fake_bind)

    async def _clear() -> None:
        provider = instance.app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    await _clear()
    monkeypatch.setattr(tai42_app, "_impl", _RealApp(instance.app))
    manifest = Manifest.model_validate(
        {
            "tools": [
                {"title": "resume", "module": RESUME_TOOL_MODULE},
                {"title": "retry", "module": RETRY_TOOL_MODULE},
            ]
        }
    )
    async with instance.app.app_context(manifest):
        yield RealWorld(
            redis=fake_redis,
            store=InteractionStore(settings.key_prefix),
            settings=settings,
            reads={"get_state": InteractionStore.get_state, "outcome_waiting": InteractionStore.outcome_waiting},
        )
    await _clear()


def operator(monkeypatch: pytest.MonkeyPatch, manager: BaseHooksManager) -> Any:
    """The hook-run operator doors (``operations.hooks``), reading ``manager`` as the current hooks manager."""
    ops = importlib.import_module("tai42_skeleton.operations.hooks")
    monkeypatch.setattr(ops, "get_hooks_manager", lambda: manager)
    return ops
