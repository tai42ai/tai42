"""The conversations router's auth boundary, pinned with access control ENABLED.

Every ``/api/conversations/*`` and ``/api/conversation-configs/*`` door reads or
mutates operator routing/config state (route rows carry execution keys and callback
secrets; the config doors are the durable per-target opt-in map), so all are AUTHED.
Each asserts an unauthenticated request is denied before the handler runs, and the
registered-stance test enumerates the doors from the router itself, so the four
``/api/conversation-configs`` doors are covered without hand-listing their stance.
"""

from __future__ import annotations

from starlette.routing import Route

import tai42_skeleton.routers.conversations as router
from tests.routers._auth_boundary import AUTHED, boundary_client

_ROUTES = [
    Route("/api/conversations", router.list_conversation_routes, methods=["GET"]),
    Route("/api/conversations/messages/failed", router.list_failed_conversations, methods=["GET"]),
    Route("/api/conversations/{route_name}", router.get_conversation_route, methods=["GET"]),
    Route("/api/conversations/{route_name}", router.create_conversation_route, methods=["POST"]),
    Route("/api/conversations/{route_name}", router.delete_conversation_route, methods=["DELETE"]),
    Route("/api/conversations/{route_name}/messages", router.send_conversation_message, methods=["POST"]),
    Route(
        "/api/conversations/{route_name}/messages/{message_id}",
        router.get_conversation_message,
        methods=["GET"],
    ),
    Route("/api/conversations/{route_name}/threads", router.list_conversation_threads, methods=["GET"]),
    Route(
        "/api/conversations/{route_name}/thread",
        router.delete_conversation_thread,
        methods=["DELETE"],
    ),
    Route("/api/conversations/{route_name}/transcript", router.get_conversation_thread, methods=["GET"]),
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
_STANCES = {
    r"/api/conversations": AUTHED,
    r"/api/conversations/messages/failed": AUTHED,
    r"/api/conversations/[^/]+": AUTHED,
    r"/api/conversations/[^/]+/messages": AUTHED,
    r"/api/conversations/[^/]+/messages/[^/]+": AUTHED,
    r"/api/conversations/[^/]+/threads": AUTHED,
    r"/api/conversations/[^/]+/thread": AUTHED,
    r"/api/conversations/[^/]+/transcript": AUTHED,
    r"/api/conversation-configs": AUTHED,
    r"/api/conversation-configs/[^/]+/[^/]+": AUTHED,
}


def test_list_routes_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.get("/api/conversations").status_code in (401, 403)


def test_list_failed_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.get("/api/conversations/messages/failed").status_code in (401, 403)


def test_get_route_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.get("/api/conversations/chat").status_code in (401, 403)


def test_create_route_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.post("/api/conversations/chat", json={}).status_code in (401, 403)


def test_delete_route_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.delete("/api/conversations/chat").status_code in (401, 403)


def test_send_message_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.post("/api/conversations/chat/messages", json={}).status_code in (401, 403)


def test_get_message_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.get("/api/conversations/chat/messages/m1").status_code in (401, 403)


def test_list_threads_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.get("/api/conversations/chat/threads").status_code in (401, 403)


def test_delete_thread_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.delete("/api/conversations/chat/thread", params={"thread_id": "bridge:chat:+1"}).status_code in (
        401,
        403,
    )


def test_get_transcript_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.get("/api/conversations/chat/transcript").status_code in (401, 403)


def test_list_configs_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.get("/api/conversation-configs").status_code in (401, 403)


def test_get_config_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.get("/api/conversation-configs/agent/assistant").status_code in (401, 403)


def test_set_config_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.put("/api/conversation-configs/agent/assistant", json={}).status_code in (401, 403)


def test_delete_config_rejected_without_auth(monkeypatch):
    client = boundary_client(monkeypatch, _ROUTES, _STANCES)
    assert client.delete("/api/conversation-configs/agent/assistant").status_code in (401, 403)


# The REAL authed stance of each registered conversations route, keyed by (path, methods).
# The tests below pin the actual ``authed`` metadata the OpenAPI emitter and the auth
# middleware both consume, so a future ``authed=False`` slip on any door — including the
# four ``/api/conversation-configs`` doors — fails here instead of shipping a
# credential-less door. The ``load_api_routes()`` enumeration is what supplies the set, so
# the config doors are covered by the mechanism, not by a hand-maintained per-door stance.
_REGISTERED_AUTHED = {
    ("/api/conversations", ("GET",)): True,
    ("/api/conversations/messages/failed", ("GET",)): True,
    ("/api/conversations/{route_name}", ("GET",)): True,
    ("/api/conversations/{route_name}", ("POST",)): True,
    ("/api/conversations/{route_name}", ("DELETE",)): True,
    ("/api/conversations/{route_name}/messages", ("POST",)): True,
    ("/api/conversations/{route_name}/messages/{message_id}", ("GET",)): True,
    ("/api/conversations/{route_name}/threads", ("GET",)): True,
    ("/api/conversations/{route_name}/thread", ("DELETE",)): True,
    ("/api/conversations/{route_name}/transcript", ("GET",)): True,
    ("/api/conversation-configs", ("GET",)): True,
    ("/api/conversation-configs/{target_kind}/{target_name}", ("GET",)): True,
    ("/api/conversation-configs/{target_kind}/{target_name}", ("PUT",)): True,
    ("/api/conversation-configs/{target_kind}/{target_name}", ("DELETE",)): True,
}


def _registered_conversation_routes():
    from tai42_skeleton.app.route_registry import load_api_routes

    return [m for m in load_api_routes() if m.path.startswith("/api/conversation")]


def test_registered_routes_match_declared_auth_stance():
    actual = {(meta.path, meta.methods): meta.authed for meta in _registered_conversation_routes()}
    assert actual == _REGISTERED_AUTHED


def test_every_registered_conversation_door_is_authed():
    """Enumerated from the router itself, so the config doors are covered without hardcoding:
    every conversations/config door the running server mounts carries ``authed=True``."""
    doors = _registered_conversation_routes()
    assert doors, "expected the conversations router to register at least one door"
    open_doors = [(m.path, m.methods) for m in doors if not m.authed]
    assert open_doors == [], f"credential-less conversation doors: {open_doors}"
