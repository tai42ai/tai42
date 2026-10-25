"""Conformance + behavior tests for the copied hooks feature.

Conformance asserts the local managers satisfy the ``tai42_contract.hooks``
``HooksManager`` protocol they are meant to implement. Behavior exercises the
in-memory registry (register / list / unregister) plus jq validation at
registration time — none of which depend on the template manager (firing, which
renders condition/expr via the template impl, is verified at integration).
"""

import re

import pytest
from tai42_contract.hooks import HookParams, HooksManager, HookSubject
from tai42_contract.template import TemplatedText
from tests._fakes.recording_storage import MappedStorage

from tai42_skeleton.app import instance
from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.managers.redis_hooks_manager import RedisHooksManager
from tai42_skeleton.hooks.settings import HooksSettings


def _settings() -> HooksSettings:
    return HooksSettings()


def test_in_memory_manager_satisfies_contract_protocol():
    manager = InMemoryHooksManager(_settings())
    assert isinstance(manager, HooksManager)


def test_redis_manager_satisfies_contract_protocol():
    manager = RedisHooksManager(_settings())
    assert isinstance(manager, HooksManager)


async def test_in_memory_register_list_unregister():
    manager = InMemoryHooksManager(_settings())
    hook = HookParams(
        name="h1",
        topic="events",
        tool="forward",
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        condition=TemplatedText(content='.status == "ready"'),
    )

    assert await manager.register(hook) is True

    assert set((await manager.list_hooks()).keys()) == {"h1"}
    by_topic = await manager.list_hooks_by_topic("events")
    assert by_topic["h1"].tool == "forward"
    assert await manager.list_hooks_by_topic("other") == {}

    assert await manager.unregister("h1") is True
    assert await manager.list_hooks() == {}
    assert await manager.unregister("h1") is False


async def test_register_rejects_invalid_jq():
    manager = InMemoryHooksManager(_settings())
    bad = HookParams(
        name="bad",
        topic="t",
        tool="noop",
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        condition=TemplatedText(content="this is ( not jq"),
    )

    with pytest.raises(ValueError, match="not valid jq"):
        await manager.register(bad)


_STORED_JQ = "jq/x"


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> MappedStorage:
    storage = MappedStorage({_STORED_JQ: ".a"})
    monkeypatch.setattr(instance.app._storage_registry, "_provider", storage)
    return storage


def _hook_naming(field: str, text: TemplatedText) -> HookParams:
    if field == "subject.key_expr":
        extra: dict[str, object] = {
            "subject": HookSubject(target_kind="tool", target_name="forward", kind="person", key_expr=text)
        }
    else:
        extra = {field: text}
    return HookParams(
        name="h",
        topic="events",
        tool="forward",
        execution_key="k-fire",
        execution_key_fingerprint="fp-fire",
        **extra,  # type: ignore[arg-type]
    )


_HOOK_SITES = ["condition", "subject.key_expr", "start_expr", "cancel_expr", "resume_expr", "extras_expr"]


@pytest.mark.parametrize("site", _HOOK_SITES)
async def test_register_refuses_a_stored_id_the_store_lacks(stored: MappedStorage, site: str) -> None:
    manager = InMemoryHooksManager(_settings())
    line = f"stored id 'jq/missing' at {site} is not a stored template"
    with pytest.raises(ValueError, match="^" + re.escape(line) + "$"):
        await manager.register(_hook_naming(site, TemplatedText(id="jq/missing")))
    assert await manager.list_hooks() == {}


@pytest.mark.parametrize("site", _HOOK_SITES)
async def test_register_stores_a_hook_whose_stored_ids_exist(stored: MappedStorage, site: str) -> None:
    manager = InMemoryHooksManager(_settings())
    assert await manager.register(_hook_naming(site, TemplatedText(id=_STORED_JQ))) is True
    assert set(await manager.list_hooks()) == {"h"}


async def test_register_refuses_a_non_canonical_id_with_the_guard_line(stored: MappedStorage) -> None:
    manager = InMemoryHooksManager(_settings())
    prefix = "stored id './x' at subject.key_expr: non-canonical store key './x'"
    with pytest.raises(ValueError, match="^" + re.escape(prefix)):
        await manager.register(_hook_naming("subject.key_expr", TemplatedText(id="./x")))
    assert await manager.list_hooks() == {}


async def test_register_with_no_storage_provider_names_the_missing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instance.app._storage_registry, "_provider", None)
    manager = InMemoryHooksManager(_settings())
    line = f"stored id '{_STORED_JQ}' at condition is not a stored template: no storage provider is installed"
    with pytest.raises(ValueError, match="^" + re.escape(line) + "$"):
        await manager.register(_hook_naming("condition", TemplatedText(id=_STORED_JQ)))
