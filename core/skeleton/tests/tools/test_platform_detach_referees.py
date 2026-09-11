"""The platform-internal state-template detach referees — each in-house door-binding holder
answering the detach gate for a ``(state, template)`` on a LIVE reference: a preset version,
a conversation config, a hook, and a schedule whose ``state_binding`` still names the
template on the state. An empty answer is no objection; the referees are armed together at
boot so the detach gate consults them by construction. Generic fixtures only (echo / status).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tai42_contract.conversations import TargetConversationConfig

from tai42_skeleton.tools import platform_referees

_BINDS_T1 = {"states": [{"state": "status", "subject_expr": {"content": ".x"}, "templates": ["t1"]}]}
_BINDS_OTHER = {"states": [{"state": "status", "subject_expr": {"content": ".x"}, "templates": ["t2"]}]}


# -- preset versions ---------------------------------------------------------


class _FakePresetStore:
    def __init__(self, versions_by_name: dict[str, list[tuple[int, dict]]]) -> None:
        self._versions = versions_by_name

    async def list_presets(self) -> list[object]:
        return [SimpleNamespace(name=name) for name in self._versions]

    async def list_versions(self, name: str) -> list[object]:
        return [SimpleNamespace(version=v, body=body) for v, body in self._versions[name]]


def _patch_preset_store(monkeypatch, versions_by_name: dict[str, list[tuple[int, dict]]]) -> None:
    import tai42_kit.db as kit_db

    from tai42_skeleton.app import instance

    monkeypatch.setattr(kit_db, "component_store_configured", lambda _c: True)
    fake_app = SimpleNamespace(presets=SimpleNamespace(store=_FakePresetStore(versions_by_name)))
    monkeypatch.setattr(instance, "app", fake_app, raising=False)


async def test_preset_referee_blocks_on_a_version_binding_the_template(monkeypatch) -> None:
    # An OLDER version binding the template blocks the detach even when the active version
    # does not — a rollback re-activates (and re-attaches) it, so it is a live reference.
    _patch_preset_store(
        monkeypatch,
        {
            "p": [(1, {"base_tool": "echo", "state_binding": _BINDS_T1}), (2, {"base_tool": "echo"})],
            "q": [(1, {"base_tool": "echo", "state_binding": _BINDS_OTHER})],
        },
    )
    assert await platform_referees._preset_detach_referee("status", "t1") == ["preset 'p' version 1"]
    # A different template / a different state draws no objection.
    assert await platform_referees._preset_detach_referee("status", "t2") == ["preset 'q' version 1"]
    assert await platform_referees._preset_detach_referee("other", "t1") == []


async def test_preset_referee_feature_off_is_empty(monkeypatch) -> None:
    import tai42_kit.db as kit_db

    monkeypatch.setattr(kit_db, "component_store_configured", lambda _c: False)
    assert await platform_referees._preset_detach_referee("status", "t1") == []


# -- conversation configs ----------------------------------------------------


def _patch_configs(monkeypatch, configs: dict[tuple[str, str], TargetConversationConfig]) -> None:
    import tai42_skeleton.conversations.cache as cache_mod
    import tai42_skeleton.conversations.target_config as target_config_mod

    class _FakeConfigStore:
        def __init__(self, *_a, **_k) -> None: ...

        async def list(self) -> dict[tuple[str, str], TargetConversationConfig]:
            return configs

    # A non-in-memory manager so the referee proceeds past its feature-off guard.
    monkeypatch.setattr(cache_mod, "get_conversations_manager", lambda: SimpleNamespace())
    monkeypatch.setattr(target_config_mod, "ConversationTargetConfigStore", _FakeConfigStore)


async def test_conversation_config_referee_blocks_on_binding_config(monkeypatch) -> None:
    from tai42_contract.states import StateBinding

    binding = StateBinding.model_validate(_BINDS_T1)
    configs = {
        ("tool", "lookup"): TargetConversationConfig(target_kind="tool", target_name="lookup", state_binding=binding),
        ("tool", "other"): TargetConversationConfig(target_kind="tool", target_name="other"),
    }
    _patch_configs(monkeypatch, configs)
    assert await platform_referees._conversation_config_detach_referee("status", "t1") == [
        "conversation config tool/lookup"
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
    assert await platform_referees._hook_detach_referee("status", "t1") == ["hook 'status-hook'"]
    assert await platform_referees._hook_detach_referee("status", "t2") == []


# -- schedules ---------------------------------------------------------------


async def test_schedule_referee_blocks_on_binding_schedule(monkeypatch) -> None:
    async def fake_export() -> list[dict[str, object]]:
        return [
            {"name": "nightly", "kwargs": {"state_binding": _BINDS_T1}},
            {"name": "weekly", "kwargs": {"state_binding": _BINDS_OTHER}},
            {"name": "plain", "kwargs": {}},  # no binding -> no objection
        ]

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    assert await platform_referees._schedule_detach_referee("status", "t1") == ["schedule 'nightly'"]
    assert await platform_referees._schedule_detach_referee("status", "t2") == ["schedule 'weekly'"]


async def test_schedule_referee_feature_off_is_empty(monkeypatch) -> None:
    from tai42_skeleton.operations import NotSupportedError

    async def fake_export() -> list[dict[str, object]]:
        raise NotSupportedError("schedules off")

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    assert await platform_referees._schedule_detach_referee("status", "t1") == []


async def test_schedule_referee_raises_on_non_list_surface(monkeypatch) -> None:
    async def fake_export() -> object:
        return {"schedules": []}

    import tai42_skeleton.operations.schedules as schedules_mod

    monkeypatch.setattr(schedules_mod, "export_schedules_raw", fake_export)
    with pytest.raises(TypeError, match="expected a list of rows"):
        await platform_referees._schedule_detach_referee("status", "t1")


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
