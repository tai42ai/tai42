"""The platform-internal state-template detach referees — each in-house door-binding holder
answering the detach gate for a ``(state, template)`` on a LIVE reference: a preset version,
a conversation config, a hook, and a schedule whose ``state_binding`` still names the
template on the state. An empty answer is no objection; the referees are armed together at
boot so the detach gate consults them by construction. Generic fixtures only (echo / status).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from tai42_contract.backend import IntervalCadence, ScheduleRecord
from tai42_contract.conversations import TargetConversationConfig
from tai42_contract.tools import ReferenceHolder
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.utils.schedule_subject import SCHEDULE_STATE_BINDING_ARG

import tai42_skeleton.versioning.store as versioning_store
from tai42_skeleton.tools import platform_referees

from ..versioning.conftest import FakeVersioningPg

_BINDS_T1 = {"states": [{"state": "status", "subject_expr": {"content": ".x"}, "templates": ["t1"]}]}
_BINDS_OTHER = {"states": [{"state": "status", "subject_expr": {"content": ".x"}, "templates": ["t2"]}]}


# -- preset versions ---------------------------------------------------------


def _patch_preset_store(monkeypatch, versions_by_name: dict[str, list[tuple[int, dict]]]) -> None:
    from tai42_skeleton.app import instance
    from tai42_skeleton.presets.store import stored_preset_body

    async def list_version_bodies() -> dict[str, list[tuple[int, object]]]:
        return {
            name: [(version, stored_preset_body(name, body)) for version, body in versions]
            for name, versions in versions_by_name.items()
        }

    fake_app = SimpleNamespace(presets=SimpleNamespace(list_version_bodies=list_version_bodies))
    monkeypatch.setattr(instance, "app", fake_app, raising=False)


async def test_preset_referee_blocks_on_a_version_binding_the_template(monkeypatch) -> None:
    # An OLDER version binding the template blocks the detach even when the active version
    # does not — a rollback re-activates (and re-attaches) it, so it is a live reference.
    _patch_preset_store(
        monkeypatch,
        {
            "p": [
                (1, {"base_tool": "echo", "description": "d", "state_binding": _BINDS_T1}),
                (2, {"base_tool": "echo", "description": "d"}),
            ],
            "q": [(1, {"base_tool": "echo", "description": "d", "state_binding": _BINDS_OTHER})],
        },
    )
    assert await platform_referees._preset_detach_referee("status", "t1") == [
        ReferenceHolder(
            kind="preset version", tag="presets", name="p", locator={"version": 1}, summary="preset 'p' version 1"
        )
    ]
    # A different template / a different state draws no objection.
    assert await platform_referees._preset_detach_referee("status", "t2") == [
        ReferenceHolder(
            kind="preset version", tag="presets", name="q", locator={"version": 1}, summary="preset 'q' version 1"
        )
    ]
    assert await platform_referees._preset_detach_referee("other", "t1") == []


async def test_preset_referee_with_no_stored_preset_is_empty(monkeypatch) -> None:
    _patch_preset_store(monkeypatch, {})
    assert await platform_referees._preset_detach_referee("status", "t1") == []


@pytest.fixture
def versioned_pg(monkeypatch: pytest.MonkeyPatch) -> FakeVersioningPg:
    """The in-memory versioned store, recording every statement it executes."""
    fake = FakeVersioningPg()

    @asynccontextmanager
    async def fake_client_ctx(
        client_cls: type, settings: object = None, **_kwargs: object
    ) -> AsyncIterator[FakeVersioningPg]:
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield fake

    monkeypatch.setattr(versioning_store, "client_ctx", fake_client_ctx)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    return fake


async def test_preset_referee_reads_every_version_of_every_preset_in_one_statement(
    versioned_pg: FakeVersioningPg,
) -> None:
    from tai42_skeleton.app import instance
    from tai42_skeleton.manifest import Manifest

    manifest = Manifest.model_validate(
        {"tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["echo"]}]}
    )
    async with instance.app.app_context(manifest):
        store = instance.app.versioning.store
        for name in ("p", "q", "r"):
            await store.create("preset", name, {"base_tool": "echo", "description": "d"})
            await store.save_version(
                "preset", name, {"base_tool": "echo", "description": "d", "state_binding": _BINDS_T1}
            )
        versioned_pg.executed.clear()
        holders = await platform_referees._preset_detach_referee("status", "t1")
    assert [(holder.name, holder.locator) for holder in holders] == [
        ("p", {"version": 2}),
        ("q", {"version": 2}),
        ("r", {"version": 2}),
    ]
    assert len(versioned_pg.executed) == 1


# -- conversation configs ----------------------------------------------------


def _patch_configs(monkeypatch, configs: dict[tuple[str, str], TargetConversationConfig]) -> None:
    import tai42_skeleton.conversations.cache as cache_mod
    from tai42_skeleton.conversations.managers.redis_conversations_manager import RedisConversationsManager
    from tai42_skeleton.conversations.settings import ConversationsSettings

    class _FakeConfigStore:
        async def list(self) -> tuple[dict[tuple[str, str], TargetConversationConfig], int]:
            return configs, 0

    # A durable manager so the referee proceeds past its feature-off guard.
    manager = RedisConversationsManager(ConversationsSettings())
    manager.target_configs = _FakeConfigStore()  # type: ignore[assignment]  # the accessor serves the double
    monkeypatch.setattr(cache_mod, "get_conversations_manager", lambda: manager)


async def test_conversation_config_referee_blocks_on_binding_config(monkeypatch) -> None:
    from tai42_contract.states import StateBinding

    binding = StateBinding.model_validate(_BINDS_T1)
    configs = {
        ("tool", "lookup"): TargetConversationConfig(target_kind="tool", target_name="lookup", state_binding=binding),
        ("tool", "other"): TargetConversationConfig(target_kind="tool", target_name="other"),
    }
    _patch_configs(monkeypatch, configs)
    assert await platform_referees._conversation_config_detach_referee("status", "t1") == [
        ReferenceHolder(
            kind="conversation config",
            tag="conversations",
            name="lookup",
            locator={"target_kind": "tool"},
            summary="conversation config tool/lookup",
        )
    ]
    assert await platform_referees._conversation_config_detach_referee("status", "t2") == []


async def test_conversation_config_referee_feature_off_is_empty(monkeypatch) -> None:
    import tai42_skeleton.conversations.cache as cache_mod
    from tai42_skeleton.conversations.managers.in_memory_conversations_manager import InMemoryConversationsManager
    from tai42_skeleton.conversations.settings import ConversationsSettings

    monkeypatch.setattr(
        cache_mod, "get_conversations_manager", lambda: InMemoryConversationsManager(ConversationsSettings())
    )
    assert await platform_referees._conversation_config_detach_referee("status", "t1") == []


# -- hooks -------------------------------------------------------------------


async def test_hook_referee_blocks_on_binding_hook(monkeypatch) -> None:
    from tai42_contract.states import StateBinding

    binding = StateBinding.model_validate(_BINDS_T1)
    hooks: dict[str, object] = {
        "h1": SimpleNamespace(name="status-hook", state_binding=binding),
        "h2": SimpleNamespace(name="other-hook", state_binding=None),
    }

    async def fake_list_hooks() -> dict[str, object]:
        return hooks

    import tai42_skeleton.hooks.cache as hooks_cache

    monkeypatch.setattr(hooks_cache, "get_hooks_manager", lambda: SimpleNamespace(list_hooks=fake_list_hooks))
    assert await platform_referees._hook_detach_referee("status", "t1") == [
        ReferenceHolder(kind="hook", tag="hooks", name="status-hook", summary="hook 'status-hook'")
    ]
    assert await platform_referees._hook_detach_referee("status", "t2") == []


# -- schedules ---------------------------------------------------------------


async def test_schedule_referee_blocks_on_binding_schedule(monkeypatch) -> None:
    # A stored schedule row carries its door binding under the kit's reserved
    # ``backend_schedule_state_binding`` key (the create door's ``state_binding`` is re-stamped
    # there, never left as a plain ``state_binding`` tool kwarg), so the referee reads it there.
    def _record(name: str, kwargs: dict) -> ScheduleRecord:
        return ScheduleRecord(name=name, tool="send", kwargs=kwargs, cadence=IntervalCadence(every=60), enabled=True)

    async def fake_export() -> list[ScheduleRecord]:
        return [
            _record("nightly", {SCHEDULE_STATE_BINDING_ARG: _BINDS_T1}),
            _record("weekly", {SCHEDULE_STATE_BINDING_ARG: _BINDS_OTHER}),
            _record("plain", {}),  # no binding -> no objection
        ]

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    assert await platform_referees._schedule_detach_referee("status", "t1") == [
        ReferenceHolder(kind="schedule", tag="schedules", name="nightly", summary="schedule 'nightly'")
    ]
    assert await platform_referees._schedule_detach_referee("status", "t2") == [
        ReferenceHolder(kind="schedule", tag="schedules", name="weekly", summary="schedule 'weekly'")
    ]


async def test_schedule_referee_feature_off_is_empty(monkeypatch) -> None:
    from tai42_skeleton.operations import NotSupportedError

    async def fake_export() -> list[ScheduleRecord]:
        raise NotSupportedError("schedules off")

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    assert await platform_referees._schedule_detach_referee("status", "t1") == []


# -- registration ------------------------------------------------------------


def test_platform_detach_referees_are_registered(monkeypatch) -> None:
    registered: list = []
    fake_app = SimpleNamespace(tools=SimpleNamespace(register_detach_referee=registered.append))
    monkeypatch.setattr(platform_referees, "tai42_app", fake_app)
    platform_referees.register_platform_detach_referees()
    assert registered == [
        platform_referees._preset_detach_referee,
        platform_referees._conversation_config_detach_referee,
        platform_referees._hook_detach_referee,
        platform_referees._schedule_detach_referee,
    ]
