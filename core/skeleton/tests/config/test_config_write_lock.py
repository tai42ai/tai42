"""The write exclusion: every ``ConfigService`` write holds it from its first store read to its persist.

The exclusion is the process-wide config-write lock, then the config store's exclusive write
span. A settings-profile apply holds both across its build, so no other write lands between the
profile's last validation and its env persist, and a profile apply waits for a write already in
its span. Both are released before the reload and broadcast tail. A write waiting for either is
recorded on its generation the way a reload-gate waiter is, so a retire the holder drives neither
counts it against the drain budget nor severs it. A write validates its ``!ENV`` markers against
the env the store converges every worker to.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest
from tai42_contract.presets import PresetBody

from tai42_skeleton.app import admission as admission_mod
from tai42_skeleton.app import applied_env, instance
from tai42_skeleton.app.admission import RequestAdmission
from tai42_skeleton.app.bus import FleetResult, LocalApplyResult
from tai42_skeleton.app.bus.ops import FleetOp
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.config.deployment_env import DeploymentOwnership
from tai42_skeleton.config.service import ConfigService

from .._fakes.bus import FakeBus
from .fake_pipeline import (
    ComboChecks,
    FakeConfigStore,
    FakeReloadAdmin,
    _no_bus,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
)

_GONE_MARKER = {"X-Race": "!ENV ${M_GONE}"}


def _manifest(url: str = "http://x") -> dict[str, Any]:
    # ``M_PICK`` picks the ``m`` server's tool, so an env write that repoints it runs the
    # held-unpick check; ``M_GONE`` is stored and live, and the profile below omits it.
    return {"mcp": [{"title": "m", "config": {"url": url}, "include": ["!ENV ${M_PICK}"]}]}


def _add_gone_marker(document: dict[str, Any]) -> None:
    document["mcp"][0]["config"]["headers"] = dict(_GONE_MARKER)


def _add_gone_marker_and_repick(document: dict[str, Any]) -> None:
    # Repicking the ``m`` server's tool runs the held-unpick listing.
    _add_gone_marker(document)
    document["mcp"][0]["include"] = ["m_z"]


def _set_secret_marker(document: dict[str, Any]) -> None:
    document["mcp"][0]["config"]["headers"] = {"X-Secret": "!ENV ${SEC}"}


class _LoggingStore(FakeConfigStore):
    """A store that logs each persisting call, in order, into a shared list."""

    def __init__(self, log: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._log = log

    def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        persisted = super().mutate_manifest(mutator)
        self._log.append("mutate_manifest")
        return persisted

    def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]:
        self._log.append("replace_manifest")
        return super().replace_manifest(document)

    def write_env(self, config: dict[str, str]) -> None:
        self._log.append("write_env")
        super().write_env(config)

    def replace_env(self, config: dict[str, str]) -> None:
        self._log.append("replace_env")
        super().replace_env(config)


class _World:
    """A shared store, services over it, and an awaited hook run inside the held-unpick listing."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _no_bus(monkeypatch)
        monkeypatch.setenv("M_PICK", "m_a")
        monkeypatch.setenv("M_GONE", "1")
        self.monkeypatch = monkeypatch
        self.log: list[str] = []
        self.store = _LoggingStore(self.log, manifest=_manifest(), env={"M_PICK": "m_a", "M_GONE": "1"})
        self.during: Callable[[], Awaitable[None]] | None = None
        self.builds: list[dict[str, str]] = []
        world = self

        async def list_active_bodies(_facet: Any) -> dict[str, PresetBody]:
            if world.during is not None:
                await world.during()
            return {}

        monkeypatch.setattr(type(instance.app.presets), "list_active_bodies", list_active_bodies)

    def service(self, *, admin: FakeReloadAdmin | None = None, bus: FakeBus | None = None) -> ConfigService:
        return ConfigService(
            config_manager=self.store,
            admin=admin or FakeReloadAdmin(),
            bus=cast("Any", bus or FakeBus()),
            combo_check=ComboChecks(),
        )

    def build(self, *, entered: asyncio.Event | None = None, release: asyncio.Event | None = None) -> Any:
        """A build that records its env, optionally blocks, then applies it to the process env."""

        async def build(env: dict[str, str], *, ownership: DeploymentOwnership) -> Epoch:
            self.builds.append(dict(env))
            if entered is not None:
                entered.set()
            if release is not None:
                await release.wait()
            # The build applies the proposed env; a stored key the profile omits leaves it.
            for key in ("M_PICK", "M_GONE"):
                if key in env:
                    self.monkeypatch.setenv(key, env[key])
                else:
                    self.monkeypatch.delenv(key, raising=False)
            return Epoch(number=0)

        return build

    async def apply_profile(self, service: ConfigService, profile_env: dict[str, str], build: Any) -> Any:
        async def save_previous(_env: dict[str, str]) -> None:
            return None

        async def release_pools() -> None:
            return None

        return await service.apply_replace_env(
            profile_env,
            save_previous=save_previous,
            build_and_swap=build,
            build_and_discard=build,
            release_llm_pools=release_pools,
        )


async def _reached(event: asyncio.Event, what: str) -> None:
    """Wait for ``event``; fail the test when it is not set within 10 s."""
    try:
        await asyncio.wait_for(event.wait(), 10)
    except TimeoutError:
        pytest.fail(f"{what} was never reached")


async def _settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


async def test_every_write_entry_waits_for_a_profile_apply_to_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    profile = asyncio.create_task(
        world.apply_profile(world.service(), {"M_PICK": "m_b"}, world.build(entered=entered, release=release))
    )
    await _reached(entered, "the profile apply's build")

    admin = FakeReloadAdmin()
    writer = world.service(admin=admin)

    async def prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        return {"SEC": "s"}, _set_secret_marker

    writes = [
        asyncio.create_task(writer.apply_change(_add_gone_marker)),
        asyncio.create_task(writer.apply_replace(_manifest("http://y"))),
        asyncio.create_task(writer.apply_env_change({"OTHER": "v"})),
        asyncio.create_task(writer.apply_env_and_change(prepare)),
    ]
    await _settle()
    assert world.log == [], "a write persisted while the profile apply was building"

    release.set()
    await profile
    marker_write, *others = await asyncio.gather(*writes, return_exceptions=True)

    # The marker on ``M_GONE`` was validated against the env the profile applied.
    assert isinstance(marker_write, ValueError)
    assert "markers dangling" in str(marker_write)
    assert "M_GONE" in str(marker_write)
    for result in others:
        assert not isinstance(result, BaseException), result
    assert world.log == ["replace_env", "replace_manifest", "write_env", "write_env", "mutate_manifest"]
    assert world.store.env_writes[0] == {"M_PICK": "m_b"}
    assert admin.calls == 3


async def test_a_profile_apply_waits_for_a_write_inside_its_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    profiler = world.service()
    validate_replace = profiler._validate_replace

    def logged_validate_replace(env: dict[str, str], ownership: DeploymentOwnership) -> None:
        world.log.append("validate_replace")
        validate_replace(env, ownership)

    monkeypatch.setattr(profiler, "_validate_replace", logged_validate_replace)
    profile: list[asyncio.Task[Any]] = []

    async def start_profile_apply() -> None:
        world.during = None
        profile.append(asyncio.create_task(world.apply_profile(profiler, {"M_PICK": "m_b"}, world.build())))
        await _settle()

    world.during = start_profile_apply
    await world.service().apply_change(_add_gone_marker_and_repick)

    with pytest.raises(ValueError, match=r"markers dangling .*M_GONE"):
        await profile[0]
    assert world.log == ["mutate_manifest", "validate_replace"]
    assert world.builds == []


def _write_lock_held() -> bool:
    lock = ConfigService._config_write_lock
    return lock is not None and lock.locked()


class _LockProbingBus(FakeBus):
    """A bus recording, at each publish, whether the config-write lock is held."""

    def __init__(self) -> None:
        super().__init__()
        self.held_at_publish: list[bool] = []

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        self.held_at_publish.append(_write_lock_held())
        return await super().publish(op, targets, local, expected_at_start=expected_at_start)


async def test_the_lock_is_free_during_every_reload_and_broadcast_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(monkeypatch)
    held_at_reload: list[bool] = []
    admin = FakeReloadAdmin(during_reload=lambda: held_at_reload.append(_write_lock_held()))
    bus = _LockProbingBus()
    service = world.service(admin=admin, bus=bus)

    async def prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        return {"SEC": "s"}, _set_secret_marker

    await service.apply_change(_add_gone_marker)
    await service.apply_replace(_manifest("http://y"))
    await service.apply_env_change({"OTHER": "v"})
    await service.apply_env_and_change(prepare)
    await world.apply_profile(service, {"M_PICK": "m_b", "M_GONE": "1", "SEC": "s"}, world.build())

    assert held_at_reload == [False, False, False, False]
    assert bus.held_at_publish == [False, False, False, False, False]


async def test_a_write_waiting_for_the_lock_is_recorded_as_waiting_on_its_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    holding, proceed = asyncio.Event(), asyncio.Event()

    async def held_prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        holding.set()
        await proceed.wait()
        return {}, lambda _document: None

    holder = asyncio.create_task(world.service().apply_env_and_change(held_prepare))
    await _reached(holding, "the holder's prepare")

    serving = Epoch(number=0)
    serving.admit()
    request = RequestAdmission(epoch=serving)
    seen: list[tuple[bool, bool, int]] = []

    async def listing() -> None:
        seen.append(
            (admission_mod.current_request_admission() is request, request in serving.waiting, serving.in_flight)
        )

    world.during = listing
    token = admission_mod._admission.set(request)
    try:
        blocked = asyncio.create_task(world.service().apply_env_change({"M_PICK": "m_b"}))
    finally:
        admission_mod._admission.reset(token)
    await _settle()

    assert request in serving.waiting
    assert serving.in_flight == 0
    proceed.set()
    await holder
    await blocked
    # The write's held-unpick check lists once it holds the lock, counted in flight again.
    assert seen
    assert set(seen) == {(True, False, 1)}


_PERSISTS = {"mutate_manifest", "replace_manifest", "write_env", "replace_env"}


class _EventBus(FakeBus):
    """A bus appending ``publish`` to the store's event list at each publish."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        self._events.append("publish")
        return await super().publish(op, targets, local, expected_at_start=expected_at_start)


def _doors(world: _World, service: ConfigService) -> dict[str, Callable[[], Awaitable[Any]]]:
    async def prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        return {"SEC": "s"}, _set_secret_marker

    return {
        "apply_change": lambda: service.apply_change(_add_gone_marker),
        "apply_replace": lambda: service.apply_replace(_manifest("http://y")),
        "apply_env_change": lambda: service.apply_env_change({"OTHER": "v"}),
        "apply_env_and_change": lambda: service.apply_env_and_change(prepare),
        "apply_replace_env": lambda: world.apply_profile(
            service, {"M_PICK": "m_b", "M_GONE": "1", "SEC": "s"}, world.build()
        ),
    }


async def test_every_write_entry_holds_the_store_span_from_its_first_read_through_its_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    events = world.store.events
    admin = FakeReloadAdmin(during_reload=lambda: events.append("reload"))
    service = world.service(admin=admin, bus=_EventBus(events))

    for name, door in _doors(world, service).items():
        events.clear()
        await door()
        assert events[0] == "exclusive_write:enter", (name, events)
        assert events.count("exclusive_write:enter") == 1, (name, events)
        span_exit = events.index("exclusive_write:exit")
        persists = [index for index, event in enumerate(events) if event in _PERSISTS]
        assert persists, (name, events)
        assert max(persists) < span_exit, (name, events)
        tail = [index for index, event in enumerate(events) if event in {"reload", "publish"}]
        assert tail, (name, events)
        assert min(tail) > span_exit, (name, events)


class _BlockingSpanStore(_LoggingStore):
    """A store whose exclusive write span blocks its thread until ``release`` is set."""

    def __init__(self, log: list[str], **kwargs: Any) -> None:
        super().__init__(log, **kwargs)
        self.blocked = threading.Event()
        self.release = threading.Event()

    @contextmanager
    def exclusive_write(self) -> Iterator[None]:
        self.blocked.set()
        if not self.release.wait(30):
            raise TimeoutError("the test never released the span")
        yield


async def test_a_write_blocked_on_the_store_span_is_recorded_as_waiting_on_its_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    store = _BlockingSpanStore(world.log, manifest=_manifest(), env={"M_PICK": "m_a", "M_GONE": "1"})
    world.store = store
    serving = Epoch(number=0)
    serving.admit()
    request = RequestAdmission(epoch=serving)
    seen: list[tuple[bool, bool, int]] = []

    async def listing() -> None:
        seen.append(
            (admission_mod.current_request_admission() is request, request in serving.waiting, serving.in_flight)
        )

    world.during = listing
    token = admission_mod._admission.set(request)
    try:
        blocked = asyncio.create_task(world.service().apply_env_change({"M_PICK": "m_b"}))
    finally:
        admission_mod._admission.reset(token)
    try:
        assert await asyncio.to_thread(store.blocked.wait, 10), "the write never entered the store span"
        assert request in serving.waiting
        assert serving.in_flight == 0
    finally:
        store.release.set()
    await blocked
    # Once it holds the span the write is ordinary in-flight work again.
    assert seen
    assert set(seen) == {(True, False, 1)}
    assert request not in serving.waiting


async def test_a_store_refusing_its_span_fails_every_write_entry_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    world.store = _LoggingStore(
        world.log,
        manifest=_manifest(),
        env={"M_PICK": "m_a", "M_GONE": "1"},
        refuse_exclusive=OSError("the config store is unreachable"),
    )
    service = world.service()

    for name, door in _doors(world, service).items():
        with pytest.raises(OSError, match="unreachable"):
            await door()
        assert world.store.events == [], name
    assert world.store.persisted == []
    assert world.store.env_writes == []


def _stale_world(monkeypatch: pytest.MonkeyPatch, manifest: dict[str, Any]) -> _World:
    """A world whose store no longer holds ``M_GONE``, which this process applied and still has.

    ``M_SYSTEM`` is process env the store never owned and no env apply set.
    """
    world = _World(monkeypatch)
    monkeypatch.setenv("M_SYSTEM", "m_c")
    monkeypatch.setattr(applied_env, "_loaded_env_keys", set())
    applied_env._apply_env({"M_PICK": "m_a", "M_GONE": "1"}, DeploymentOwnership())
    assert "M_GONE" in applied_env.applied_env_keys()
    world.store.manifest = manifest
    world.store.env = {"M_PICK": "m_a"}
    return world


def _system_marker(document: dict[str, Any]) -> None:
    document["mcp"][0]["config"]["headers"] = {"X-System": "!ENV ${M_SYSTEM}"}


def _with_headers(headers: dict[str, str]) -> dict[str, Any]:
    document = _manifest()
    document["mcp"][0]["config"]["headers"] = headers
    return document


async def test_a_manifest_marker_on_a_key_the_store_dropped_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _stale_world(monkeypatch, _manifest())
    service = world.service()

    with pytest.raises(ValueError, match=r"markers dangling .*M_GONE"):
        await service.apply_change(_add_gone_marker)
    assert world.store.persisted == []

    await service.apply_change(_system_marker)
    assert world.store.manifest["mcp"][0]["config"]["headers"] == {"X-System": "!ENV ${M_SYSTEM}"}


async def test_an_env_write_over_a_marker_on_a_key_the_store_dropped_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _stale_world(monkeypatch, _with_headers(dict(_GONE_MARKER)))
    with pytest.raises(ValueError, match=r"markers dangling .*M_GONE"):
        await world.service().apply_env_change({"OTHER": "v"})
    assert world.store.env_writes == []

    world = _stale_world(monkeypatch, _with_headers({"X-System": "!ENV ${M_SYSTEM}"}))
    await world.service().apply_env_change({"OTHER": "v"})
    assert world.store.env_writes == [{"OTHER": "v"}]


async def test_a_profile_over_a_marker_on_a_key_the_store_dropped_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _stale_world(monkeypatch, _with_headers(dict(_GONE_MARKER)))
    with pytest.raises(ValueError, match=r"markers dangling .*M_GONE"):
        await world.apply_profile(world.service(), {"M_PICK": "m_b"}, world.build())
    assert world.builds == []
    assert world.store.env_writes == []

    world = _stale_world(monkeypatch, _with_headers({"X-System": "!ENV ${M_SYSTEM}"}))
    await world.apply_profile(world.service(), {"M_PICK": "m_b"}, world.build())
    assert world.store.env_writes == [{"M_PICK": "m_b"}]


_PROMPT_S = 2.0


class _CountingSpanStore(_LoggingStore):
    """A conforming store counting each entry attempt and each completed exit of its span.

    With ``hold_next_exit`` set, the next span's exit blocks its thread, still holding the span,
    until ``release_exit`` is set.
    """

    def __init__(self, log: list[str], **kwargs: Any) -> None:
        super().__init__(log, **kwargs)
        self.entering = threading.Semaphore(0)
        self.exited = threading.Semaphore(0)
        self.hold_next_exit = False
        self.exiting = threading.Event()
        self.release_exit = threading.Event()

    @contextmanager
    def exclusive_write(self) -> Iterator[None]:
        self.entering.release()
        try:
            with super().exclusive_write():
                try:
                    yield
                finally:
                    if self.hold_next_exit:
                        self.hold_next_exit = False
                        self.exiting.set()
                        if not self.release_exit.wait(30):
                            raise TimeoutError("the test never released the span's exit")
        finally:
            self.exited.release()


def _spans_never_overlap(events: list[str]) -> list[list[str]]:
    """Split ``events`` into the spans it records, asserting no span enters before the last one exited."""
    spans: list[list[str]] = []
    current: list[str] | None = None
    for event in events:
        if event == "exclusive_write:enter":
            assert current is None, f"a span entered while another was held: {events}"
            current = []
        elif event == "exclusive_write:exit":
            assert current is not None, events
            spans.append(current)
            current = None
        else:
            assert current is not None, f"{event} ran outside a span: {events}"
            current.append(event)
    assert current is None, events
    return spans


class _ForeignHolder:
    """Holds the store's span on its own thread, as another process sharing the store would."""

    def __init__(self, store: FakeConfigStore) -> None:
        self._store = store
        self._holding = threading.Event()
        self.release = threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        with self._store.span_lock:
            self._holding.set()
            self.release.wait(30)

    def start(self) -> None:
        self._thread.start()
        assert self._holding.wait(10), "the foreign holder never took the span"

    def holding(self) -> bool:
        return self._thread.is_alive()


async def _await_semaphore(semaphore: threading.Semaphore) -> None:
    assert await asyncio.to_thread(semaphore.acquire, timeout=10), "the span was never reached"


async def test_a_writer_cancelled_on_entry_returns_promptly_and_the_next_writer_waits_for_the_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    store = _CountingSpanStore(world.log, manifest=_manifest(), env={"M_PICK": "m_a", "M_GONE": "1"})
    world.store = store
    foreign = _ForeignHolder(store)
    foreign.start()
    try:
        first = asyncio.create_task(world.service().apply_change(_add_gone_marker))
        await _await_semaphore(store.entering)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(_PROMPT_S):
                await first
        assert foreign.holding()

        second = asyncio.create_task(world.service().apply_env_change({"OTHER": "v"}))
        await _await_semaphore(store.entering)
        await _settle()
        assert not second.done()
        assert store.events == [], "a writer entered the span while another holder held it"
        assert world.log == []
    finally:
        foreign.release.set()
    await second
    await _await_semaphore(store.exited)
    await _await_semaphore(store.exited)

    # The abandoned span is entered and exited with nothing inside it; the second writer reads
    # and persists inside its own span, and the two are never held at once.
    abandoned, written = sorted(_spans_never_overlap(store.events), key=len)
    assert abandoned == []
    assert written[-1] == "write_env"
    assert "read_env" in written
    assert world.log == ["write_env"]
    assert store.env_writes == [{"OTHER": "v"}]


async def test_a_writer_cancelled_again_during_its_exit_returns_only_once_the_span_is_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch)
    store = _CountingSpanStore(world.log, manifest=_manifest(), env={"M_PICK": "m_a", "M_GONE": "1"})
    world.store = store
    inside = asyncio.Event()

    async def listing() -> None:
        world.during = None
        inside.set()
        await asyncio.Event().wait()

    world.during = listing
    store.hold_next_exit = True
    first = asyncio.create_task(world.service().apply_env_change({"M_PICK": "m_b"}))
    await _reached(inside, "the writer's listing")
    first.cancel()
    assert await asyncio.to_thread(store.exiting.wait, 10), "the span's exit never started"
    first.cancel()
    second = asyncio.create_task(world.service().apply_env_change({"OTHER": "v"}))
    await _settle()
    try:
        assert not first.done(), "the writer returned while its span's exit was still running"
        assert store.events.count("exclusive_write:enter") == 1
    finally:
        store.release_exit.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second

    spans = _spans_never_overlap(store.events)
    assert len(spans) == 2
    assert "write_env" not in spans[0]
    assert spans[1][-1] == "write_env"
    assert world.log == ["write_env"]
    assert store.env_writes == [{"OTHER": "v"}]
