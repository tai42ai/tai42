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
from tai42_contract.interactions import AnswerFormat, InteractionRequest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import instance
from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions.store import _PENDING_LIST_DEFAULT_LIMIT
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


# The referee reads each schedule's dispatch target from the exported record's
# ``kwargs[tool_name_arg]`` — the literal key the worker pops and fires — so an
# OPERATOR-NAMED schedule (a name outside the derived ``<tool>_<hex>`` convention) that
# fires the renamed tool is caught, not silently missed.
_TOOL_ARG = "backend_tool_name"


def _record(name: str, tool: str, *, enabled: bool = True) -> dict[str, object]:
    return {
        "name": name,
        "args": [],
        "kwargs": {_TOOL_ARG: tool},
        "schedule": {"__type__": "interval", "every": 30.0, "relative": False},
        "enabled": enabled,
    }


async def test_schedule_referee_blocks_on_operator_named_schedule(monkeypatch) -> None:
    """An operator-named schedule (name NOT following ``<tool>_<hex>``) whose kwargs
    fire ``echo`` blocks a rename of ``echo``; a disabled such schedule counts too (it
    is re-enable-able); a schedule firing a different tool does not."""

    async def fake_export() -> list[dict[str, object]]:
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


async def test_schedule_referee_empty_when_no_match(monkeypatch) -> None:
    async def fake_export() -> list[dict[str, object]]:
        return [_record("alerts-run", "alerts"), _record("echoes", "echoish")]

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    assert await platform_referees._schedule_referee("echo") == []


async def test_schedule_referee_feature_off_is_empty(monkeypatch) -> None:
    from tai42_skeleton.operations import NotSupportedError

    async def fake_export() -> list[dict[str, object]]:
        raise NotSupportedError("schedules off")

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    assert await platform_referees._schedule_referee("echo") == []


async def test_schedule_referee_raises_when_export_tool_absent_with_markers_present(monkeypatch) -> None:
    """The backend is installed (marker tools present) but registers no
    ``backend_export_schedules``: its schedules' dispatch targets cannot be read, so the
    rename is blocked LOUDLY — never a silent "no holders"."""
    from tai42_skeleton.tools.binding import UnknownToolError

    async def fake_export() -> list[dict[str, object]]:
        raise UnknownToolError("backend_export_schedules")

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    with pytest.raises(RuntimeError, match="registers no 'backend_export_schedules'"):
        await platform_referees._schedule_referee("echo")


async def test_schedule_referee_raises_on_non_list_surface(monkeypatch) -> None:
    """An unreadable holder store must FAIL the rename, never silently drop every
    schedule holder: a non-list export shape raises."""

    async def fake_export() -> object:
        return {"schedules": []}  # a mapping, not the expected list of rows

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    with pytest.raises(TypeError, match="expected a list of rows"):
        await platform_referees._schedule_referee("echo")


async def test_schedule_referee_raises_on_uninterpretable_row(monkeypatch) -> None:
    """A record the referee cannot interpret raises rather than being silently skipped —
    a skipped holder is a stranded rename: a non-dict row, a nameless row, and a record
    whose kwargs carry no dispatch target each raise."""

    async def fake_non_dict_row() -> list[object]:
        return ["echo"]

    async def fake_nameless_row() -> list[dict[str, object]]:
        return [{"schedule_id": 7}]

    async def fake_no_target_row() -> list[dict[str, object]]:
        # A record with a name but no ``tool_name_arg`` in kwargs is an unreadable holder.
        return [{"name": "orphan", "kwargs": {}}]

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_non_dict_row)
    with pytest.raises(TypeError, match="expected a dict"):
        await platform_referees._schedule_referee("echo")

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_nameless_row)
    with pytest.raises(TypeError, match="no string 'name'"):
        await platform_referees._schedule_referee("echo")

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_no_target_row)
    with pytest.raises(TypeError, match=r"no string .* dispatch target"):
        await platform_referees._schedule_referee("echo")


async def test_schedule_referee_missing_target_error_names_the_dispatch_contract(monkeypatch) -> None:
    """A backend whose ``*_TOOL_NAME_ARG`` disagrees with the host's ``BACKEND_TOOL_NAME_ARG``
    injects the target under a different key, so the referee finds no target on the first
    record and (fail-safe) over-blocks EVERY rename. The over-block stays loud, but the error
    must be DIAGNOSABLE: it names the expected key and the dispatch contract both sides must
    agree on, so a misconfiguration is fixable rather than a bare shape error."""

    async def fake_mismatched_key() -> list[dict[str, object]]:
        # The backend injected the target under a DIFFERENT key than the host reads.
        return [{"name": "nightly-cleanup", "kwargs": {"other_tool_name": "echo"}}]

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_mismatched_key)
    with pytest.raises(TypeError) as excinfo:
        await platform_referees._schedule_referee("echo")
    message = str(excinfo.value)
    # Names the expected key, the host env var, and the backend side to realign.
    assert repr(_TOOL_ARG) in message
    assert "BACKEND_TOOL_NAME_ARG" in message
    assert "*_TOOL_NAME_ARG" in message


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

    async def fake_list_routes() -> dict[str, object]:
        return routes

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
