"""Every door that schedules a turn refuses during a config reload, before anything is written.

The three turn entry points — the channel ``accept``, the api-door ``submit_api_message`` and the
event-door ``submit_event`` — refuse first with the retriable contract ``ServerReloadingError``, so a
channel door (a plugin's included) answers the reload ``503`` through the app's error handler and no
record, claim or caps slot is left behind.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient
from tai42_contract.conversations import ConversationEvent, ConversationEventSubmission
from tai42_contract.errors import ErrorKind, ServerReloadingError, error_kind

import tai42_skeleton.conversations as conversations
from tai42_skeleton.app.reload_gate import REJECT_MESSAGE
from tai42_skeleton.conversations.turn import admission, api_door, event_door, intake


def _untouchable(*_args, **_kwargs):
    raise AssertionError("a refused turn must read or write nothing")


@pytest.fixture
def reloading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the reload gate, and make every store, route and caps read a loud failure."""
    monkeypatch.setattr(admission, "reload_gate", SimpleNamespace(locked=True))
    for module in (intake, api_door, event_door):
        monkeypatch.setattr(module.cache, "get_conversations_manager", _untouchable)
        if hasattr(module, "get_turn_caps"):
            monkeypatch.setattr(module, "get_turn_caps", _untouchable)


async def _never_connected() -> bool:
    return False


async def test_accept_refuses_while_reloading(reloading) -> None:
    with pytest.raises(ServerReloadingError, match="reloading") as excinfo:
        await intake.accept("synthetic", "line-1", "addr-1", "addr-1", "hello", "provider-1")
    assert error_kind(excinfo.value) is ErrorKind.UNAVAILABLE


async def test_submit_api_message_refuses_while_reloading(reloading) -> None:
    with pytest.raises(ServerReloadingError):
        await api_door.submit_api_message(
            "synthetic-route", "user-1", "hello", "caller-1", 0, client_connected=_never_connected
        )


async def test_submit_event_refuses_while_reloading(reloading) -> None:
    submission = ConversationEventSubmission(
        thread_id="bridge:synthetic-route:addr-1", event=ConversationEvent(event_id="e-1", kind="ping")
    )
    with pytest.raises(ServerReloadingError):
        await event_door.submit_event("synthetic-route", submission, "caller-1", client_connected=_never_connected)


async def test_the_gate_released_admits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission, "reload_gate", SimpleNamespace(locked=False))
    admission.refuse_turn_while_reloading()


def test_a_plugin_channel_door_answers_the_reload_503(reloading) -> None:
    """A neutral channel door that lets the refusal propagate answers the retriable 503."""
    from tai42_skeleton.app.server import _install_error_handlers

    async def synthetic_inbound(request: Request) -> Response:
        try:
            await conversations.accept("synthetic", "line-1", "addr-1", "addr-1", "hello", "provider-1")
        except (ValueError, LookupError):
            # The channel acknowledges and drops bad input; a reload refusal is neither.
            return JSONResponse({"status": "ignored"})
        return JSONResponse({"status": "accepted"})

    app = Starlette(routes=[Route("/synthetic/inbound", synthetic_inbound, methods=["POST"])])
    _install_error_handlers(app)
    response = TestClient(app, raise_server_exceptions=False).post("/synthetic/inbound")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json() == {"error": REJECT_MESSAGE, "reloading": True}
