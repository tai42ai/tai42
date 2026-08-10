"""The per-target config doors' HTTP edge: the PUT body/path reconciliation, the CRUD
envelope, and the loud 400/404 refusals."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tai42_skeleton.conversations import target_config as store_module

from .fake_config_redis import FakeConfigRedis, make_config_client_ctx


class _FakeAgents:
    def all_agents(self) -> dict[str, object]:
        return {"assistant": object()}


class _FakeTools:
    async def get_tool(self, key: str) -> object:
        from tai42_skeleton.tools.binding import UnknownToolError

        raise UnknownToolError(key)


class _FakeApp:
    agents = _FakeAgents()
    tools = _FakeTools()


def _router():
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app.instance import app as skeleton_app

    with tai42_app.bound(skeleton_app):
        from tai42_skeleton.routers import conversations as router

    return router


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")
    fake = FakeConfigRedis()
    monkeypatch.setattr(store_module, "client_ctx", make_config_client_ctx(fake))
    router = _router()
    op_globals = router._set_conversation_config_op.__globals__
    monkeypatch.setitem(op_globals, "get_conversations_manager", lambda: object())
    from tai42_skeleton.app import instance

    monkeypatch.setattr(instance, "app", _FakeApp(), raising=False)
    routes = [
        Route("/api/conversation-configs", router.list_conversation_configs, methods=["GET"]),
        Route(
            "/api/conversation-configs/{target_kind}/{target_name}",
            router.get_conversation_config,
            methods=["GET"],
        ),
        Route(
            "/api/conversation-configs/{target_kind}/{target_name}",
            router.set_conversation_config,
            methods=["PUT"],
        ),
        Route(
            "/api/conversation-configs/{target_kind}/{target_name}",
            router.delete_conversation_config,
            methods=["DELETE"],
        ),
    ]
    return TestClient(Starlette(routes=routes))


def test_set_get_list_delete_round_trip(client):
    put = client.put(
        "/api/conversation-configs/agent/assistant",
        json={"multichannel": True, "greeting_template": "hi {pairing_code}"},
    )
    assert put.status_code == 200, put.text
    assert put.json()["data"]["created"] is True

    got = client.get("/api/conversation-configs/agent/assistant")
    assert got.status_code == 200
    assert got.json()["data"]["multichannel"] is True

    listed = client.get("/api/conversation-configs")
    assert listed.json()["data"]["total"] == 1

    deleted = client.delete("/api/conversation-configs/agent/assistant")
    assert deleted.status_code == 200
    assert deleted.json()["data"]["removed"] is True


def test_a_body_key_disagreeing_with_the_path_is_a_400(client):
    resp = client.put(
        "/api/conversation-configs/agent/assistant",
        json={"target_name": "someone-else", "multichannel": True},
    )
    assert resp.status_code == 400
    assert "must match" in resp.json()["error"]


def test_an_invalid_json_body_is_a_400(client):
    resp = client.put(
        "/api/conversation-configs/agent/assistant",
        content="not json at all",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert "invalid JSON body" in resp.json()["error"]


def test_a_body_key_agreeing_with_the_path_takes_the_success_path(client):
    resp = client.put(
        "/api/conversation-configs/agent/assistant",
        json={"target_kind": "agent", "target_name": "assistant", "multichannel": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["created"] is True


def test_a_non_object_body_is_a_400(client):
    resp = client.put("/api/conversation-configs/agent/assistant", json=["not", "an", "object"])
    assert resp.status_code == 400


def test_an_unknown_placeholder_is_a_400(client):
    resp = client.put("/api/conversation-configs/agent/assistant", json={"greeting_template": "hi {name}"})
    assert resp.status_code == 400
    assert "pairing_code" in resp.json()["error"]


def test_a_put_to_an_unknown_target_is_a_404(client):
    resp = client.put("/api/conversation-configs/agent/ghost", json={"multichannel": True})
    assert resp.status_code == 404


def test_get_unknown_is_a_404(client):
    resp = client.get("/api/conversation-configs/agent/assistant")
    assert resp.status_code == 404


def test_delete_unknown_is_a_404(client):
    resp = client.delete("/api/conversation-configs/agent/assistant")
    assert resp.status_code == 404
