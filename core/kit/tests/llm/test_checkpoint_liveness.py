"""The live-thread filter registry: one test per consumer, every filter asked, a claim mapped to its owner."""

from __future__ import annotations

from collections.abc import Collection, Sequence

import pytest

from tai42_kit.llm.checkpoint import liveness
from tai42_kit.llm.checkpoint.liveness import live_thread_filters, live_threads, register_live_thread_filter


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch):
    monkeypatch.setattr(liveness, "_filters", {})


def _claiming(ids: set[str], calls: list[tuple[str, str | None, list[str]]] | None = None):
    async def _filter(provider: str, conn_string: str | None, thread_ids: Sequence[str]) -> Collection[str]:
        if calls is not None:
            calls.append((provider, conn_string, list(thread_ids)))
        return {thread_id for thread_id in thread_ids if thread_id in ids}

    return _filter


def test_a_second_registration_under_one_owner_is_refused():
    register_live_thread_filter("probe", _claiming(set()))
    with pytest.raises(ValueError, match="live-thread filter 'probe' is already registered"):
        register_live_thread_filter("probe", _claiming(set()))


@pytest.mark.parametrize("owner", ["", "Probe", "1probe", "pro be", "probe!"])
def test_a_malformed_owner_is_refused(owner):
    with pytest.raises(ValueError, match="must match"):
        register_live_thread_filter(owner, _claiming(set()))


def test_the_registered_filters_are_a_read_only_view():
    fn = _claiming(set())
    register_live_thread_filter("probe-a_1", fn)
    view = live_thread_filters()
    assert dict(view) == {"probe-a_1": fn}
    with pytest.raises(TypeError):
        view["other"] = fn  # type: ignore[index]


async def test_every_filter_is_asked_and_each_claim_maps_to_its_owner():
    calls_a: list[tuple[str, str | None, list[str]]] = []
    calls_b: list[tuple[str, str | None, list[str]]] = []
    register_live_thread_filter("alpha", _claiming({"t1", "t3"}, calls_a))
    register_live_thread_filter("beta", _claiming({"t3", "t4"}, calls_b))
    claimed = await live_threads("postgres", "postgresql://h/db", ["t1", "t2", "t3", "t4"])
    assert claimed == {"t1": "alpha", "t3": "alpha", "t4": "beta"}
    assert calls_a == [("postgres", "postgresql://h/db", ["t1", "t2", "t3", "t4"])]
    assert calls_b == calls_a


async def test_a_claim_outside_the_candidates_is_ignored():
    async def _overclaiming(provider, conn_string, thread_ids):
        return ["not-a-candidate", *thread_ids[:1]]

    register_live_thread_filter("over", _overclaiming)
    assert await live_threads("memory", None, ["t1", "t2"]) == {"t1": "over"}


async def test_no_candidates_asks_no_filter():
    calls: list[tuple[str, str | None, list[str]]] = []
    register_live_thread_filter("alpha", _claiming({"t1"}, calls))
    assert await live_threads("memory", None, []) == {}
    assert calls == []


async def test_a_filter_raise_propagates():
    async def _broken(provider, conn_string, thread_ids):
        raise RuntimeError("liveness store unreachable")

    register_live_thread_filter("alpha", _claiming({"t1"}))
    register_live_thread_filter("broken", _broken)
    with pytest.raises(RuntimeError, match="liveness store unreachable"):
        await live_threads("redis", None, ["t1"])
