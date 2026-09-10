"""Every door's HTTP-edge extractor must thread ``state_binding`` as the parsed model — the
routes must not silently drop a body binding the operations layer expects. Born-red: before
the fix each extractor omitted (or mis-typed) the key."""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from tai42_contract.states import StateBinding

from tai42_skeleton.routers import conversations as conversations_router
from tai42_skeleton.routers import hooks as hooks_router
from tai42_skeleton.routers import schedules as schedules_router

_BINDING = {"states": [{"state": "status", "subject_expr": ".x"}]}


def _request(body: Any, **path_params: str) -> Request:
    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/x",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "path_params": path_params,
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


async def test_schedule_create_extractor_threads_the_binding() -> None:
    kwargs = await schedules_router._extract_create(_request({"tool_name": "t", "state_binding": _BINDING}))
    assert isinstance(kwargs["state_binding"], StateBinding)
    assert kwargs["state_binding"] == StateBinding.model_validate(_BINDING)


async def test_schedule_create_extractor_binding_absent_is_none() -> None:
    kwargs = await schedules_router._extract_create(_request({"tool_name": "t"}))
    assert kwargs["state_binding"] is None


async def test_hook_register_extractor_threads_the_binding() -> None:
    body = {"name": "h", "topic": "t", "tool": "x", "execution_key": "k", "state_binding": _BINDING}
    flat = await hooks_router._extract_hook_params(_request(body))
    assert isinstance(flat["state_binding"], StateBinding)
    assert flat["state_binding"] == StateBinding.model_validate(_BINDING)


async def test_conversation_config_extractor_threads_the_binding() -> None:
    flat = await conversations_router._extract_target_config(
        _request({"state_binding": _BINDING}, target_kind="tool", target_name="n")
    )
    assert isinstance(flat["state_binding"], StateBinding)
    assert flat["state_binding"] == StateBinding.model_validate(_BINDING)
