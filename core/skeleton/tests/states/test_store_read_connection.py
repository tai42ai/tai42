"""Every stand-alone states-store read runs on the kit's ``read_connection``; a write never does.

A counting wrapper replaces ``read_connection`` in each store module, so every case counts the
autocommit read connections one call opens. Driven against the in-memory fake Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tai42_contract.states.models import StateSubject

from tai42_skeleton.states.store import PostgresStatesStore
from tai42_skeleton.states.store import attachments as attachments_module
from tai42_skeleton.states.store import connection as connection_module
from tai42_skeleton.states.store import declarations as declarations_module
from tai42_skeleton.states.store import queries as queries_module
from tai42_skeleton.states.store import records as records_module
from tai42_skeleton.states.store import templates as templates_module

from .conftest import FakeStatesPg

_SUBJECT = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t1")


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Every read connection a call opens, as the autocommit flag it carried inside."""
    seen: list[bool] = []
    real = connection_module.read_connection

    @asynccontextmanager
    async def counting(pool: Any) -> AsyncIterator[Any]:
        async with real(pool) as conn:
            seen.append(conn.autocommit)
            yield conn

    for module in (attachments_module, connection_module, declarations_module, queries_module, records_module):
        monkeypatch.setattr(module, "read_connection", counting)
    monkeypatch.setattr(templates_module, "read_connection", counting)
    return seen


_READS: dict[str, Callable[[PostgresStatesStore], Awaitable[Any]]] = {
    "get_declaration": lambda s: s.get_declaration("alerts"),
    "list_declarations": lambda s: s.list_declarations(),
    "count_records": lambda s: s.count_records("alerts"),
    "count_records_for_target": lambda s: s.count_records_for_target("agent", "a"),
    "field_stats": lambda s: s.field_stats("alerts"),
    "get_template": lambda s: s.get_template("tpl"),
    "list_templates": lambda s: s.list_templates(),
    "attached_template_counts": lambda s: s.attached_template_counts(),
    "get_attachment": lambda s: s.get_attachment("alerts", "tpl"),
    "list_attachments_for_state": lambda s: s.list_attachments_for_state("alerts"),
    "list_attachments_of_template": lambda s: s.list_attachments_of_template("tpl"),
    "list_all_attachments": lambda s: s.list_all_attachments(),
    "read_record": lambda s: s.read_record("alerts", _SUBJECT),
    "read_record_view": lambda s: s.read_record_view("alerts", _SUBJECT),
    "export_records": lambda s: s.export_records("alerts"),
    "list_aliases": lambda s: s.list_aliases("alerts"),
    "list_subjects": lambda s: s.list_subjects("alerts", kind=None, limit=10, cursor=None),
    "search_records": lambda s: s.search_records("alerts", {"n": 1}, limit=10, cursor=None),
    "writes": lambda s: s.writes("alerts", _SUBJECT, limit=10, cursor=None),
    "op_applied": lambda s: s.op_applied("op-1"),
}


@pytest.mark.parametrize("method", sorted(_READS))
async def test_a_standalone_read_opens_one_autocommit_read_connection(
    method: str, pg: FakeStatesPg, store: PostgresStatesStore, opened: list[bool]
) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "t1", {"n": 1})
    opened.clear()

    await _READS[method](store)

    assert opened == [True]


async def test_a_read_joining_a_callers_transaction_opens_none(
    pg: FakeStatesPg, store: PostgresStatesStore, opened: list[bool]
) -> None:
    pg.seed_declaration("alerts")
    async with store.begin() as conn:
        await store.read_record_view("alerts", _SUBJECT, conn=conn)

    assert opened == []


async def test_a_write_opens_no_read_connection(
    pg: FakeStatesPg, store: PostgresStatesStore, opened: list[bool]
) -> None:
    await store.upsert_template("tpl", {"type": "object"}, None)

    assert opened == []


async def test_a_folded_key_reads_the_surviving_record(
    pg: FakeStatesPg, store: PostgresStatesStore, opened: list[bool]
) -> None:
    pg.seed_declaration("alerts")
    pg.seed_record("alerts", "agent", "a", "thread", "new", {"n": 9})
    pg.aliases[("alerts", "agent", "a", "thread", "old")] = {
        "state": "alerts",
        "target_kind": "agent",
        "target_name": "a",
        "alias_kind": "thread",
        "alias_key": "old",
        "canonical_kind": "thread",
        "canonical_key": "new",
        "mode": "switch",
    }

    data, _seq = await store.read_record(
        "alerts", StateSubject(target_kind="agent", target_name="a", kind="thread", key="old")
    )

    assert data == {"n": 9}
    assert opened == [True]
