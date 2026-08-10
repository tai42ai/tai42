"""The per-target config operations: set (model-validate + target-exists + upsert),
get/list, delete, the key/placeholder refusals, and the backend-off 501."""

from __future__ import annotations

import pytest

from tai42_skeleton.conversations import target_config as store_module
from tai42_skeleton.conversations.managers.in_memory_conversations_manager import InMemoryConversationsManager
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import BadRequestError, NotFoundError, NotSupportedError

from .fake_config_redis import FakeConfigRedis, make_config_client_ctx


class _FakeAgents:
    def __init__(self, names: set[str]) -> None:
        self._names = names

    def all_agents(self) -> dict[str, object]:
        return {name: object() for name in self._names}


class _FakeTools:
    def __init__(self, names: set[str]) -> None:
        self._names = names

    async def get_tool(self, key: str) -> object:
        from tai42_skeleton.tools.binding import UnknownToolError

        if key not in self._names:
            raise UnknownToolError(key)
        return object()


class _FakeApp:
    def __init__(self, agents: set[str], tools: set[str]) -> None:
        self.agents = _FakeAgents(agents)
        self.tools = _FakeTools(tools)


@pytest.fixture
def wired(monkeypatch) -> FakeConfigRedis:
    """Backend ON, config store over a fake redis, an agent ``assistant`` and a tool
    ``lookup`` registered — the standard happy-path environment."""
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")
    fake = FakeConfigRedis()
    monkeypatch.setattr(store_module, "client_ctx", make_config_client_ctx(fake))
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: object())
    from tai42_skeleton.app import instance

    monkeypatch.setattr(instance, "app", _FakeApp({"assistant"}, {"lookup"}), raising=False)
    return fake


async def test_set_creates_then_upserts(wired):
    created = await ops.set_conversation_config("agent", "assistant")
    assert created["created"] is True
    assert created["config"] == {
        "target_kind": "agent",
        "target_name": "assistant",
        "multichannel": False,
        "greeting_template": None,
    }
    replaced = await ops.set_conversation_config(
        "agent", "assistant", multichannel=True, greeting_template="hi {pairing_code}"
    )
    assert replaced["created"] is False
    got = await ops.get_conversation_config("agent", "assistant")
    assert got["multichannel"] is True
    assert got["greeting_template"] == "hi {pairing_code}"


async def test_set_on_a_tool_target(wired):
    result = await ops.set_conversation_config("tool", "lookup", multichannel=True)
    assert result["created"] is True


async def test_list_returns_items_and_total(wired):
    await ops.set_conversation_config("agent", "assistant")
    await ops.set_conversation_config("tool", "lookup")
    listed = await ops.list_conversation_configs()
    assert listed["total"] == 2
    assert {(item["target_kind"], item["target_name"]) for item in listed["items"]} == {
        ("agent", "assistant"),
        ("tool", "lookup"),
    }


async def test_set_refuses_an_unknown_agent(wired):
    with pytest.raises(NotFoundError, match="agent not found"):
        await ops.set_conversation_config("agent", "ghost")


async def test_set_refuses_an_unknown_tool(wired):
    with pytest.raises(NotFoundError, match="tool not found"):
        await ops.set_conversation_config("tool", "ghost")


async def test_set_refuses_an_unknown_placeholder(wired):
    with pytest.raises(BadRequestError, match="pairing_code"):
        await ops.set_conversation_config("agent", "assistant", greeting_template="hi {name}")


async def test_set_refuses_a_blank_greeting(wired):
    with pytest.raises(BadRequestError, match="greeting_template"):
        await ops.set_conversation_config("agent", "assistant", greeting_template="   ")


async def test_set_refuses_an_unknown_target_kind(wired):
    with pytest.raises(BadRequestError):
        await ops.set_conversation_config("robot", "assistant")


async def test_get_unknown_is_404(wired):
    with pytest.raises(NotFoundError, match="conversation config not found"):
        await ops.get_conversation_config("agent", "assistant")


async def test_get_malformed_kind_is_400(wired):
    with pytest.raises(BadRequestError, match="target_kind"):
        await ops.get_conversation_config("robot", "assistant")


async def test_delete_removes_then_404s(wired):
    await ops.set_conversation_config("agent", "assistant")
    removed = await ops.delete_conversation_config("agent", "assistant")
    assert removed == {"removed": True, "target_kind": "agent", "target_name": "assistant"}
    with pytest.raises(NotFoundError):
        await ops.delete_conversation_config("agent", "assistant")


@pytest.fixture
def gated_off(monkeypatch) -> None:
    """Backend OFF — the in-memory manager, so every config op refuses with the 501 the
    route ops give, before any store is touched."""
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: InMemoryConversationsManager(ConversationsSettings()))


async def test_list_gated_off_is_501(gated_off):
    with pytest.raises(NotSupportedError):
        await ops.list_conversation_configs()


async def test_get_gated_off_is_501(gated_off):
    with pytest.raises(NotSupportedError):
        await ops.get_conversation_config("agent", "assistant")


async def test_set_gated_off_is_501(gated_off, monkeypatch):
    # The model validates fine; the 501 is the backend gate, raised before the store.
    from tai42_skeleton.app import instance

    monkeypatch.setattr(instance, "app", _FakeApp({"assistant"}, set()), raising=False)
    with pytest.raises(NotSupportedError):
        await ops.set_conversation_config("agent", "assistant")


async def test_delete_gated_off_is_501(gated_off):
    with pytest.raises(NotSupportedError):
        await ops.delete_conversation_config("agent", "assistant")
