"""The tool-rename referee seam and the platform-internal referees.

Covers the ``ToolRenameRefereeRegistry`` body behind
``app.tools.register_rename_referee`` (register / duplicate-raise / snapshot / reset),
the facet round-trip, and each platform-internal referee answering the rename gate on a
LIVE reference: a schedule firing the name, a hook targeting it, a conversation route
pointing at it, a tool-extensions map entry carrying it, and a parked interaction whose
resume continuation is it. The parks referee walks the FULL ``pending:expiry`` index — a
target park seeded BEYOND the ``list_pending`` cap is still found, proving the referee
never inherits that audit's 500-member ceiling.

Generic fixtures only (echo / alerts / acme). A referee raising is a LOUD failure the
rename gate must never swallow — asserted at the seam.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tai42_contract.backend import IntervalCadence, ScheduleRecord
from tai42_contract.interactions import AnswerFormat, InteractionRequest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import instance
from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions.store.reads import _PENDING_LIST_DEFAULT_LIMIT
from tai42_skeleton.tools import platform_referees
from tai42_skeleton.tools.rename_referees import ToolRenameRefereeRegistry

# -- registry ----------------------------------------------------------------


async def _empty(_name: str) -> list[str]:
    return []


async def _holder(_name: str) -> list[str]:
    return ["something"]


def test_registry_collects_and_snapshots() -> None:
    reg = ToolRenameRefereeRegistry()
    assert reg.all() == []
    reg.register(_empty)
    reg.register(_holder)
    assert reg.all() == [_empty, _holder]
    # ``all()`` is a snapshot copy — mutating it never touches the registry.
    reg.all().clear()
    assert reg.all() == [_empty, _holder]


def test_registry_rejects_duplicate_provider() -> None:
    reg = ToolRenameRefereeRegistry()
    reg.register(_empty)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_empty)


def test_registry_reset_clears() -> None:
    reg = ToolRenameRefereeRegistry()
    reg.register(_empty)
    reg.reset()
    assert reg.all() == []


# The facet round-trip (``app.tools.register_rename_referee`` / ``rename_referees()``)
# is exercised through a live app in ``tests/operations/test_rename_referees_ops.py``,
# where a booted ``app_context`` guarantees a serving core for the facet to forward to.


# -- platform referee: schedules ---------------------------------------------


# The referee reads each schedule's fired tool from the exported ``ScheduleRecord.tool`` —
# the backend splits it out of its own tool-name key, whatever that key is — so an
# OPERATOR-NAMED schedule (a name outside the derived ``<tool>_<hex>`` convention) that
# fires the renamed tool is caught, not silently missed.
_BACKEND_OWN_TOOL_KEY = "fake_backend_target"


def _record(name: str, tool: str, *, enabled: bool = True) -> ScheduleRecord:
    return ScheduleRecord(
        name=name,
        tool=tool,
        kwargs={"to": "x"},
        cadence=IntervalCadence(every=30.0),
        enabled=enabled,
    )


async def test_schedule_referee_blocks_on_operator_named_schedule(monkeypatch) -> None:
    """An operator-named schedule (name NOT following ``<tool>_<hex>``) firing ``echo``
    blocks a rename of ``echo``; a disabled such schedule counts too (it is re-enable-able);
    a schedule firing a different tool does not."""

    async def fake_export() -> list[ScheduleRecord]:
        return [
            _record("nightly-cleanup", "echo"),  # operator-named, fires echo -> blocks
            _record("weekly-digest", "echo", enabled=False),  # disabled but re-enable-able -> blocks
            _record("echo_0123456789ab", "alerts"),  # echo-shaped NAME but fires alerts -> no block
            _record("alerts-run", "alerts"),  # unrelated tool -> no block
        ]

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    holders = await platform_referees._schedule_referee("echo")
    assert holders == ["schedule 'nightly-cleanup'", "schedule 'weekly-digest'"]


async def test_schedule_referee_reads_the_tool_field_whatever_the_backend_key(monkeypatch) -> None:
    """The record's kwargs carry no tool-name key at all (the backend split its own,
    non-default key out); the referee reads ``tool``."""

    async def fake_export() -> list[ScheduleRecord]:
        record = _record("nightly", "echo")
        assert _BACKEND_OWN_TOOL_KEY not in record.kwargs
        assert "backend_tool_name" not in record.kwargs
        return [record]

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    assert await platform_referees._schedule_referee("echo") == ["schedule 'nightly'"]


async def test_schedule_referee_empty_when_no_match(monkeypatch) -> None:
    async def fake_export() -> list[ScheduleRecord]:
        return [_record("alerts-run", "alerts"), _record("echoes", "echoish")]

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    assert await platform_referees._schedule_referee("echo") == []


async def test_schedule_referee_feature_off_is_empty(monkeypatch) -> None:
    from tai42_skeleton.operations import NotSupportedError

    async def fake_export() -> list[ScheduleRecord]:
        raise NotSupportedError("schedules off")

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    assert await platform_referees._schedule_referee("echo") == []


async def test_schedule_referee_raises_when_export_tool_absent_with_markers_present(monkeypatch) -> None:
    """The backend is installed (marker tools present) but registers no
    ``backend_export_schedules``: its schedules' dispatch targets cannot be read, so the
    rename is blocked LOUDLY — never a silent "no holders"."""
    from tai42_skeleton.tools.binding import UnknownToolError

    async def fake_export() -> list[ScheduleRecord]:
        raise UnknownToolError("backend_export_schedules")

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    with pytest.raises(RuntimeError, match="registers no 'backend_export_schedules'"):
        await platform_referees._schedule_referee("echo")


async def test_schedule_referee_propagates_an_unreadable_export(monkeypatch) -> None:
    """An export that is not ``list[ScheduleRecord]`` fails inside ``export_schedules_raw``;
    the referee lets it propagate, so the rename is blocked loudly."""
    from tai42_skeleton.operations import OperationFailedError

    async def fake_export() -> list[ScheduleRecord]:
        raise OperationFailedError("records that are not ScheduleRecord")

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    with pytest.raises(OperationFailedError):
        await platform_referees._schedule_referee("echo")


# -- platform referee: hooks -------------------------------------------------


async def test_hook_referee_blocks_on_matching_tool(monkeypatch) -> None:
    hooks: dict[str, object] = {
        "h1": SimpleNamespace(name="alerts-hook", tool="echo"),
        "h2": SimpleNamespace(name="other-hook", tool="alerts"),
    }

    async def fake_list_hooks() -> dict[str, object]:
        return hooks

    import tai42_skeleton.hooks.cache as hooks_cache

    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: SimpleNamespace(list_hooks=fake_list_hooks))
    holders = await platform_referees._hook_referee("echo")
    assert holders == ["hook 'alerts-hook'"]


# -- platform referee: conversation routes -----------------------------------


async def test_conversation_route_referee_blocks_on_tool_target(monkeypatch) -> None:
    routes: dict[str, object] = {
        "r1": SimpleNamespace(route_name="acme-route", target_kind="tool", target_name="echo"),
        "r2": SimpleNamespace(route_name="agent-route", target_kind="agent", target_name="echo"),
        "r3": SimpleNamespace(route_name="other-route", target_kind="tool", target_name="alerts"),
    }

    async def fake_list_routes() -> tuple[dict[str, object], int]:
        return routes, 0

    import tai42_skeleton.conversations as conversations_mod

    monkeypatch.setattr(
        conversations_mod, "get_conversations_manager", lambda: SimpleNamespace(list_routes=fake_list_routes)
    )
    holders = await platform_referees._conversation_route_referee("echo")
    # Only the tool-kind route targeting the name blocks; an agent-kind route with the
    # same target_name does not.
    assert holders == ["conversation route 'acme-route'"]


# -- platform referee: tool extensions ---------------------------------------


def _fake_app_with_extensions(tool_extensions: dict[str, object]):
    return SimpleNamespace(admin=SimpleNamespace(live_manifest_typed=SimpleNamespace(tool_extensions=tool_extensions)))


async def test_tool_extensions_referee_blocks_on_map_entry(monkeypatch) -> None:
    monkeypatch.setattr(instance, "app", _fake_app_with_extensions({"echo": [["ext-a"]]}), raising=False)
    assert await platform_referees._tool_extensions_referee("echo") == ["tool-extensions map entry for 'echo'"]


async def test_tool_extensions_referee_empty_when_absent(monkeypatch) -> None:
    monkeypatch.setattr(instance, "app", _fake_app_with_extensions({"alerts": [["ext-a"]]}), raising=False)
    assert await platform_referees._tool_extensions_referee("echo") == []


# -- platform referee: parked interactions -----------------------------------


def _park(store: InteractionStore, *, iid: str, gid: str, continuation_tool: str, expiry_at: datetime):
    now = datetime.now(UTC)
    return InteractionRequest(
        interaction_id=iid,
        group_id=gid,
        question="proceed?",
        answer_format=AnswerFormat.TEXT,
        reply_to=store.reply_key(iid),
        created_at=now,
        timeout_at=expiry_at,
        mode="async",
        continuation_tool=continuation_tool,
        continuation_identity="svc-key",
        expiry_at=expiry_at,
    )


async def test_parked_referee_walks_full_index_beyond_list_pending_cap(fake_redis, monkeypatch) -> None:
    """A park resuming into the renamed tool must block even when it sits BEYOND the
    ``list_pending`` default cap: seed ``_PENDING_LIST_DEFAULT_LIMIT`` filler parks at the
    soonest deadlines and the ONE target park at the latest, so a capped scan would list
    only the filler and miss the target. The referee walks the whole ``pending:expiry``
    index and finds it; ``list_pending`` (capped, and omitting ``continuation_tool``)
    demonstrably does not."""
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()

    store = InteractionStore("interactions:")
    now = datetime.now(UTC)
    # Filler parks at the soonest deadlines (ranks 0..cap-1), none naming the tool.
    for i in range(_PENDING_LIST_DEFAULT_LIMIT):
        await store.add(
            fake_redis,
            _park(
                store,
                iid=f"filler-{i}",
                gid=f"g-{i}",
                continuation_tool="other_tool",
                expiry_at=now + timedelta(minutes=1 + i),
            ),
            idle_ttl=86400,
        )
    # The target park at the LATEST deadline — rank == cap, one past the ceiling.
    target_expiry = now + timedelta(days=365)
    await store.add(
        fake_redis,
        _park(store, iid="target", gid="g-target", continuation_tool="echo", expiry_at=target_expiry),
        idle_ttl=86400,
    )

    # The capped audit misses the target (it is one rank past the ceiling).
    listed = {item["interaction_id"] for item in await store.list_pending(fake_redis, now=now)}
    assert "target" not in listed
    assert len(listed) == _PENDING_LIST_DEFAULT_LIMIT

    # The full walk sees every park's continuation, including the beyond-cap target.
    tools = await store.parked_continuation_tools(fake_redis)
    assert len(tools) == _PENDING_LIST_DEFAULT_LIMIT + 1
    assert "echo" in tools

    # The referee, reading through the store's client seam, blocks on the target park.
    import tai42_kit.clients as kit_clients

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake_redis

    monkeypatch.setattr(kit_clients, "client_ctx", fake_client_ctx)
    holders = await platform_referees._parked_interaction_referee("echo")
    assert holders == ["1 parked interaction park resuming into 'echo'"]
    # A name no park resumes into draws no objection.
    assert await platform_referees._parked_interaction_referee("alerts") == []


async def test_parked_referee_blocks_on_answered_continuation_awaiting_redelivery(fake_redis, monkeypatch) -> None:
    """An ANSWERED (or expired) park is removed from ``pending:expiry`` and lives only in
    the durable ``continuation:due`` outbox the reaper still re-fires as
    ``run_tool(<tool>)``. That tool is a live holder the pending-only walk misses, so a
    rename landing between answer and redelivery would strand the continuation. Seed the
    outbox record alone (NO ``pending:expiry`` membership) and prove the referee, unioning
    both indices, blocks the rename that the old pending-only walk would have let through."""
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()

    store = InteractionStore("interactions:")
    # The post-answer, pre-redelivery state: a continuation-due record (tool="echo") and
    # its index member, with nothing in pending:expiry.
    await fake_redis.hset(
        store.continuation_due_key("answered-1"),
        mapping={"tool": "echo", "identity": "svc-key", "fingerprint": "", "answer": '"ok"', "attempts": "0"},
    )
    await fake_redis.zadd(store.continuation_due_index_key, {"answered-1": 0})

    # The pending-only walk sees nothing; the outbox walk surfaces the answered tool.
    assert await store.parked_continuation_tools(fake_redis) == []
    assert await store.continuation_due_tools(fake_redis) == ["echo"]

    import tai42_kit.clients as kit_clients

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake_redis

    monkeypatch.setattr(kit_clients, "client_ctx", fake_client_ctx)
    holders = await platform_referees._parked_interaction_referee("echo")
    assert holders == ["1 answered continuation awaiting redelivery into 'echo'"]
    # A name no continuation resumes into draws no objection.
    assert await platform_referees._parked_interaction_referee("alerts") == []


async def test_continuation_due_tools_raises_on_torn_record(fake_redis, monkeypatch) -> None:
    """A continuation-due member whose record hash TTL-expired is genuinely gone and is
    skipped; a PRESENT record missing its ``tool`` field is torn and raises loudly — never
    a silent drop that would let a stranding rename through."""
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()

    store = InteractionStore("interactions:")
    # A member with no backing record hash (TTL-expired) — skipped, not raised.
    await fake_redis.zadd(store.continuation_due_index_key, {"gone-1": 0})
    assert await store.continuation_due_tools(fake_redis) == []

    # A present-but-torn record (no 'tool' field) — raises.
    await fake_redis.hset(store.continuation_due_key("torn-1"), mapping={"identity": "svc-key", "attempts": "0"})
    await fake_redis.zadd(store.continuation_due_index_key, {"torn-1": 0})
    with pytest.raises(RuntimeError, match="no 'tool' field"):
        await store.continuation_due_tools(fake_redis)


async def test_parked_referee_feature_off_is_empty(monkeypatch) -> None:
    monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    reset_all_settings()
    assert await platform_referees._parked_interaction_referee("echo") == []


# -- states referee ----------------------------------------------------------


async def test_states_referee_blocks_on_records_under_the_tool_target(monkeypatch) -> None:
    import tai42_skeleton.states.db as states_db
    import tai42_skeleton.states.store as states_store

    monkeypatch.setattr(states_db, "states_store_configured", lambda: True)

    async def _count(self, target_kind: str, target_name: str) -> int:
        assert target_kind == "tool"
        return 3 if target_name == "echo" else 0

    monkeypatch.setattr(states_store.PostgresStatesStore, "count_records_for_target", _count)
    holders = await platform_referees._states_referee("echo")
    assert holders == ["3 state records under target tool/echo"]
    assert await platform_referees._states_referee("other") == []


async def test_states_referee_singular_record_wording(monkeypatch) -> None:
    import tai42_skeleton.states.db as states_db
    import tai42_skeleton.states.store as states_store

    monkeypatch.setattr(states_db, "states_store_configured", lambda: True)

    async def _count(self, target_kind: str, target_name: str) -> int:
        return 1

    monkeypatch.setattr(states_store.PostgresStatesStore, "count_records_for_target", _count)
    assert await platform_referees._states_referee("echo") == ["1 state record under target tool/echo"]


async def test_states_referee_feature_off_is_empty(monkeypatch) -> None:
    import tai42_skeleton.states.db as states_db

    monkeypatch.setattr(states_db, "states_store_configured", lambda: False)
    assert await platform_referees._states_referee("echo") == []


def test_states_referee_is_registered(monkeypatch) -> None:
    # The platform arms the states referee alongside the other in-house holders, so a
    # rename gate consults it by construction.
    registered: list = []
    fake_app = SimpleNamespace(tools=SimpleNamespace(register_rename_referee=registered.append))
    monkeypatch.setattr(platform_referees, "tai42_app", fake_app)
    platform_referees.register_platform_rename_referees()
    assert platform_referees._states_referee in registered
