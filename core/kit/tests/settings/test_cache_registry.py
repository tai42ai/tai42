"""settings_cache registry: cached accessors register for the global reset,
caches clear before custom hooks run, and hooks see fresh values. The epoch
stamp + sweep find a retired-epoch instance a holder kept alive, and skip
primitives."""

import gc
from typing import cast

import pytest
from pydantic_settings import BaseSettings

from tai42_kit.clients.base import advance_client_epoch, current_client_epoch
from tai42_kit.settings import cache_registry
from tai42_kit.settings.cache_registry import (
    _EPOCH_STAMP_ATTR,
    register_settings_reset,
    reset_all_settings,
    settings_cache,
    sweep_stale_settings,
)


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot the global registry + stamp roster so test state doesn't leak.

    Restored in this fixture's own teardown, before the suite's settings reset runs,
    so a hook a test registers never reaches that reset.
    """
    saved = (cache_registry._CACHE_CLEARS, cache_registry._RESET_HOOKS, cache_registry._stamp_roster)
    cache_registry._CACHE_CLEARS = dict(saved[0])
    cache_registry._RESET_HOOKS = dict(saved[1])
    cache_registry._stamp_roster = list(saved[2])
    yield
    cache_registry._CACHE_CLEARS, cache_registry._RESET_HOOKS, cache_registry._stamp_roster = saved


def test_settings_cache_caches_and_resets():
    calls = []

    @settings_cache
    def accessor() -> int:
        calls.append(1)
        return len(calls)

    assert accessor() == 1
    assert accessor() == 1
    reset_all_settings()
    assert accessor() == 2


def test_reset_clears_caches_before_hooks():
    order = []

    @settings_cache
    def accessor() -> str:
        order.append("accessor-built")
        return "value"

    accessor()
    order.clear()

    @register_settings_reset
    def hook() -> None:
        order.append("hook")
        accessor()

    reset_all_settings()
    assert order == ["hook", "accessor-built"]


def test_reregistration_is_idempotent():
    # A module re-import re-runs the decorator with a new function object of
    # the same qualified name — it must replace, not accumulate.
    calls = []

    def make():
        @register_settings_reset
        def hook() -> None:
            calls.append(1)

        return hook

    make()
    make()
    reset_all_settings()
    assert sum(calls) == 1


def test_hook_failure_propagates():
    @register_settings_reset
    def broken() -> None:
        raise RuntimeError("bad settings")

    with pytest.raises(RuntimeError, match="bad settings"):
        reset_all_settings()


def _of_type(findings, cls):
    suffix = f".{cls.__qualname__}"
    return [h for h in findings if h.settings_type.endswith(suffix)]


def test_sweep_finds_stale_instance_held_by_a_holder():
    class _HeldSettings(BaseSettings):
        value: int = 1

    holder = {}

    @settings_cache
    def accessor() -> _HeldSettings:
        return _HeldSettings()

    holder["s"] = accessor()  # constructed + stamped at epoch 0, kept alive
    retired = advance_client_epoch()

    found = _of_type(sweep_stale_settings(retired), _HeldSettings)
    assert len(found) == 1
    assert found[0].epoch == retired
    assert found[0].holders  # gc.get_referrers named at least one holder


def test_sweep_ignores_released_instance():
    class _FreedSettings(BaseSettings):
        value: int = 1

    @settings_cache
    def accessor() -> _FreedSettings:
        return _FreedSettings()

    accessor()  # constructed + stamped, held only by the lru_cache
    retired = advance_client_epoch()
    reset_all_settings()  # clears the cache -> the instance is released
    gc.collect()

    assert _of_type(sweep_stale_settings(retired), _FreedSettings) == []


def test_stamp_is_invisible_to_model_dump():
    class _DumpedSettings(BaseSettings):
        value: int = 7

    @settings_cache
    def accessor() -> _DumpedSettings:
        return _DumpedSettings()

    inst = accessor()
    assert _EPOCH_STAMP_ATTR not in inst.model_dump()  # stamp excluded from dumps
    assert getattr(inst, _EPOCH_STAMP_ATTR) == 0  # but present, at the birth epoch


def test_primitive_returns_are_not_stamped():
    @settings_cache
    def config_mode() -> str:
        return "prod"

    before = len(cache_registry._stamp_roster)
    assert config_mode() == "prod"  # a non-model accessor works and is exempt
    assert len(cache_registry._stamp_roster) == before  # nothing rostered


def test_sweep_is_epoch_selective():
    class _OldSettings(BaseSettings):
        value: int = 1

    class _NewSettings(BaseSettings):
        value: int = 1

    old = _OldSettings()
    cache_registry._stamp_settings(old)  # stamped at epoch 0
    advance_client_epoch()  # current epoch 1
    new = _NewSettings()
    cache_registry._stamp_settings(new)  # stamped at epoch 1
    advance_client_epoch()  # current epoch 2, so epoch 1 is now genuinely retired

    found = sweep_stale_settings(1)
    # Only the epoch-1 instance matches retired_epoch=1; the epoch-0 one is skipped.
    assert _of_type(found, _NewSettings)
    assert not _of_type(found, _OldSettings)
    assert old.value == 1  # keep both instances alive to the sweep above
    assert new.value == 1


def test_sweep_tolerates_dead_roster_refs():
    import weakref

    class _Ghost(BaseSettings):
        value: int = 1

    ghost = _Ghost()
    dead = cast("weakref.ref[BaseSettings]", weakref.ref(ghost))
    del ghost
    gc.collect()
    assert dead() is None  # referent gone; the ref lingers in the roster
    cache_registry._stamp_roster.append(dead)
    # The sweep skips the dead ref instead of dereferencing None.
    sweep_stale_settings(advance_client_epoch())


def test_sweep_rejects_current_epoch():
    # The current epoch is never "retired": sweeping it would flag every live
    # current instance as a leak, so it is rejected loudly.
    with pytest.raises(ValueError, match="retired epoch"):
        sweep_stale_settings(current_client_epoch())


def test_sweep_rejects_future_epoch():
    with pytest.raises(ValueError, match="retired epoch"):
        sweep_stale_settings(current_client_epoch() + 5)


def test_concurrent_stamp_and_sweep_do_not_corrupt_roster():
    import threading

    class _Conc(BaseSettings):
        value: int = 1

    def _live_count() -> int:
        return sum(1 for ref in cache_registry._stamp_roster if ref() is not None)

    baseline = _live_count()
    held: list[_Conc] = []  # keep every stamped instance alive so no ref is pruned
    per_thread = 50
    n_threads = 8
    barrier = threading.Barrier(n_threads + 1)

    def stamp() -> None:
        barrier.wait()
        for _ in range(per_thread):
            inst = _Conc()
            held.append(inst)
            cache_registry._stamp_settings(inst)

    threads = [threading.Thread(target=stamp) for _ in range(n_threads)]
    for t in threads:
        t.start()
    barrier.wait()
    # Sweep concurrently against the ongoing appends; -1 is a retired epoch that
    # matches no stamp, so this only exercises the lock-guarded roster snapshot.
    for _ in range(per_thread):
        assert sweep_stale_settings(-1) == []
    for t in threads:
        t.join()

    # Every stamped, still-held instance is rostered exactly once (no lost or
    # duplicated append under concurrency).
    assert _live_count() == baseline + n_threads * per_thread


def test_unhashable_settings_are_rostered_by_weakref():
    class _UnhashableSettings(BaseSettings):
        value: int = 1

    @settings_cache
    def accessor() -> _UnhashableSettings:
        return _UnhashableSettings()

    inst = accessor()
    with pytest.raises(TypeError):
        hash(inst)  # the unhashable shape the roster (a list of weakrefs) tolerates
    retired = advance_client_epoch()
    assert _of_type(sweep_stale_settings(retired), _UnhashableSettings)


def test_sweep_does_not_report_an_instance_only_cyclic_garbage_keeps():
    class _CycledSettings(BaseSettings):
        value: int = 1

    gc.collect()
    gc.disable()  # keep the cycle uncollected until the sweep looks
    try:
        cycle: list[object] = [_CycledSettings()]
        cycle.append(cycle)
        cache_registry._stamp_settings(cycle[0])
        retired = advance_client_epoch()
        del cycle  # now unreachable: only the cycle itself keeps the instance
        found = _of_type(sweep_stale_settings(retired), _CycledSettings)
    finally:
        gc.enable()

    assert found == []


def _hold_while_sweeping(accessor) -> list:
    held = accessor()
    retired = advance_client_epoch()
    found = sweep_stale_settings(retired)
    assert held.value == 1
    return found


def test_sweep_names_an_executing_frame_holding_the_instance(caplog):
    class _FrameHeldSettings(BaseSettings):
        value: int = 1

    @settings_cache
    def accessor() -> _FrameHeldSettings:
        return _FrameHeldSettings()

    found = _of_type(_hold_while_sweeping(accessor), _FrameHeldSettings)
    reset_all_settings()

    assert len(found) == 1
    frame_holders = [h for h in found[0].holders if h.startswith("frame ")]
    assert len(frame_holders) == 1
    assert frame_holders[0].startswith("frame _hold_while_sweeping (")
    assert frame_holders[0].endswith(f"{__file__}:{_hold_while_sweeping.__code__.co_firstlineno + 3})")
    assert "<unknown>" not in caplog.text
    assert "frame _hold_while_sweeping (" in caplog.text


async def _hold_across_await(accessor, gate) -> None:
    held = accessor()
    await gate
    assert held.value == 1


async def test_sweep_names_a_suspended_coroutine_holding_the_instance():
    import asyncio

    class _CoroHeldSettings(BaseSettings):
        value: int = 1

    @settings_cache
    def accessor() -> _CoroHeldSettings:
        return _CoroHeldSettings()

    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(_hold_across_await(accessor, gate))
    await asyncio.sleep(0)  # the task now waits on the gate, holding its settings
    retired = advance_client_epoch()
    reset_all_settings()
    found = _of_type(sweep_stale_settings(retired), _CoroHeldSettings)
    gate.set_result(None)
    await task

    assert len(found) == 1
    line = _hold_across_await.__code__.co_firstlineno + 2
    assert f"coroutine _hold_across_await ({__file__}:{line})" in found[0].holders


def _hold_across_yield(accessor):
    held = accessor()
    yield
    assert held.value == 1


def test_sweep_names_a_suspended_generator_holding_the_instance():
    class _GenHeldSettings(BaseSettings):
        value: int = 1

    @settings_cache
    def accessor() -> _GenHeldSettings:
        return _GenHeldSettings()

    suspended = _hold_across_yield(accessor)
    next(suspended)  # the generator now waits at its yield, holding its settings
    retired = advance_client_epoch()
    reset_all_settings()
    found = _of_type(sweep_stale_settings(retired), _GenHeldSettings)
    with pytest.raises(StopIteration):
        next(suspended)

    assert len(found) == 1
    line = _hold_across_yield.__code__.co_firstlineno + 2
    assert f"generator _hold_across_yield ({__file__}:{line})" in found[0].holders


async def _hold_across_async_yield(accessor):
    held = accessor()
    yield
    assert held.value == 1


async def test_sweep_names_a_suspended_async_generator_holding_the_instance():
    class _AsyncGenHeldSettings(BaseSettings):
        value: int = 1

    @settings_cache
    def accessor() -> _AsyncGenHeldSettings:
        return _AsyncGenHeldSettings()

    suspended = _hold_across_async_yield(accessor)
    await anext(suspended)  # the async generator now waits at its yield, holding its settings
    retired = advance_client_epoch()
    reset_all_settings()
    found = _of_type(sweep_stale_settings(retired), _AsyncGenHeldSettings)
    with pytest.raises(StopAsyncIteration):
        await anext(suspended)

    assert len(found) == 1
    line = _hold_across_async_yield.__code__.co_firstlineno + 2
    assert f"async generator _hold_across_async_yield ({__file__}:{line})" in found[0].holders


_KEPT_ERRORS: list[BaseException] = []
_KEPT_SETTINGS: list[BaseSettings] = []


def _raise_holding(accessor) -> None:
    held = accessor()
    raise RuntimeError(f"failed with {held.value}")


def test_sweep_names_a_finished_frame_a_kept_traceback_holds():
    class _TracebackHeldSettings(BaseSettings):
        value: int = 1

    @settings_cache
    def accessor() -> _TracebackHeldSettings:
        return _TracebackHeldSettings()

    try:
        _raise_holding(accessor)
    except RuntimeError as exc:
        _KEPT_ERRORS.append(exc)  # a live traceback keeps the finished frame and its locals
    try:
        retired = advance_client_epoch()
        reset_all_settings()
        found = _of_type(sweep_stale_settings(retired), _TracebackHeldSettings)
    finally:
        _KEPT_ERRORS.clear()

    assert len(found) == 1
    line = _raise_holding.__code__.co_firstlineno + 2
    assert f"frame _raise_holding ({__file__}:{line})" in found[0].holders


def test_sweep_still_reports_a_module_level_reference_after_collecting():
    class _ModuleHeldSettings(BaseSettings):
        value: int = 1

    @settings_cache
    def accessor() -> _ModuleHeldSettings:
        return _ModuleHeldSettings()

    _KEPT_SETTINGS.append(accessor())
    try:
        retired = advance_client_epoch()
        reset_all_settings()
        found = _of_type(sweep_stale_settings(retired), _ModuleHeldSettings)
    finally:
        _KEPT_SETTINGS.clear()

    assert len(found) == 1
    assert found[0].holders == ("builtins.list",)
