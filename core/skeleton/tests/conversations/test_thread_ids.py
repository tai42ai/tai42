"""The bridge thread-id grammar's one home, and the person-thread reads built on it."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest
from tai42_contract.errors import ErrorKind, error_kind

from tai42_skeleton.conversations.person_thread import newest_person_thread_record, person_routes
from tai42_skeleton.conversations.thread_ids import (
    BRIDGE_THREAD_PREFIX,
    PERSON_THREAD_PREFIX,
    PersonThreadId,
    RouteThreadId,
    ThreadIdError,
    parse_thread_id,
    person_id_of,
    person_thread_id,
    route_thread_id,
)


def test_the_builders_spell_the_grammar() -> None:
    assert BRIDGE_THREAD_PREFIX == "bridge:"
    assert PERSON_THREAD_PREFIX == "bridge:@person:"
    assert route_thread_id("chat-line", "+15550001111") == "bridge:chat-line:+15550001111"
    assert person_thread_id("p-1") == "bridge:@person:p-1"


@pytest.mark.parametrize(
    ("thread_id", "parsed"),
    [
        ("bridge:chat-line:+15550001111", RouteThreadId(route_name="chat-line", client_address="+15550001111")),
        # The route segment carries no ``:``, so an address holding ``:`` stays whole.
        ("bridge:chat-line:alice/user:7", RouteThreadId(route_name="chat-line", client_address="alice/user:7")),
        ("bridge:@person:p-1", PersonThreadId(person_id="p-1")),
    ],
)
def test_parse_reads_route_and_person_threads(thread_id: str, parsed: RouteThreadId | PersonThreadId) -> None:
    assert parse_thread_id(thread_id) == parsed


@pytest.mark.parametrize(
    "thread_id",
    [
        "bridge:@person:",  # a person thread naming no person
        "chat-line:addr",  # outside the reserved namespace
        "bridge:chat-line:",  # an empty address
        "bridge::addr",  # an empty route
        "bridge:@person",  # ``@person`` is never a route; with no address it is no thread
    ],
)
def test_parse_refuses_ids_outside_the_grammar(thread_id: str) -> None:
    with pytest.raises(ThreadIdError) as excinfo:
        parse_thread_id(thread_id)
    assert isinstance(excinfo.value, ValueError)
    assert error_kind(excinfo.value) is ErrorKind.BAD_INPUT


def test_parse_round_trips_the_builders() -> None:
    assert parse_thread_id(route_thread_id("r", "a:b")) == RouteThreadId("r", "a:b")
    assert parse_thread_id(person_thread_id("pid")) == PersonThreadId("pid")


@pytest.mark.parametrize(
    ("thread_id", "person"),
    [("bridge:@person:p-1", "p-1"), ("bridge:chat:addr", None), ("bridge:@person:", None), ("other", None)],
)
def test_person_id_of(thread_id: str, person: str | None) -> None:
    assert person_id_of(thread_id) == person


@pytest.mark.parametrize(
    "first",
    ["tai42_skeleton.agent.binding", "tai42_skeleton.conversations"],
)
def test_the_grammar_imports_without_a_cycle_in_either_order(first: str) -> None:
    second = (
        "tai42_skeleton.conversations" if first == "tai42_skeleton.agent.binding" else "tai42_skeleton.agent.binding"
    )
    code = f"import {first}; import {second}; import tai42_skeleton.conversations.thread_ids"
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=120)


def _person(*address_routes: list[str]) -> SimpleNamespace:
    return SimpleNamespace(addresses=[SimpleNamespace(routes=routes) for routes in address_routes])


def test_person_routes_spans_every_address() -> None:
    assert person_routes(_person(["a", "b"], ["b", "c"])) == {"a", "b", "c"}  # type: ignore[arg-type]


class _Store:
    def __init__(self, records: list) -> None:
        self.records = records
        self.calls: list = []

    async def list_person_thread_records(self, route_names, thread_id, *, offset, limit, newest_first=False, q=None):
        self.calls.append((route_names, thread_id, offset, limit, newest_first))
        return SimpleNamespace(records=self.records[:limit]), 0


async def test_newest_person_thread_record_reads_one_newest_row() -> None:
    newest = SimpleNamespace(route_name="b", client_address="x")
    store = _Store([newest])
    record = await newest_person_thread_record(store, _person(["b", "a"]), "bridge:@person:p")  # type: ignore[arg-type]
    assert record is newest
    assert store.calls == [(["a", "b"], "bridge:@person:p", 0, 1, True)]


async def test_newest_person_thread_record_is_none_for_an_empty_thread() -> None:
    assert await newest_person_thread_record(_Store([]), _person(["a"]), "bridge:@person:p") is None  # type: ignore[arg-type]
