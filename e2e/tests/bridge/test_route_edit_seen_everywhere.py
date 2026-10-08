"""A route edit made through one replica is seen by the next message on every process.

Each server process serves its route reads from a snapshot of the route table, re-loaded whenever
the table's write token changes; every route write sets a fresh token in the same atomic step. So a
route created, replaced or deleted through replica B is seen by the very next message the API door
takes on replica A, and by an in-process door tool the backend worker runs.

The route's tool target records the marker its ``start_expr`` builds, so the record names the route
body that handled each message: ``created:`` before the replace, ``replaced:`` after.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

pytestmark = pytest.mark.needs(
    "process",
    "kind:identity",
    "probe-tools",
    "store:redis",
    "setting:seeded-access-control",
    "setting:conversations:redis",
    "setting:tool:builtin-doors",
)

# An https callback with nothing behind it: the answer lands on the route's record (poll).
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


def _api(bridge_stack: tuple[TaiStack, str], port: int) -> ApiClient:
    stack, root_token = bridge_stack
    return stack.api(port).with_token(root_token)


async def _put_route(api: ApiClient, route_name: str, execution_key: str, body: str) -> None:
    await api.post(
        f"/api/conversations/{route_name}",
        json={
            "door": "api",
            "target_kind": "tool",
            "target_name": "e2e_extras_probe",
            "execution_key": execution_key,
            "callback_url": _UNREACHABLE_CALLBACK,
            "start_expr": {
                "content": f'{{marker: ("{body}:" + (if .event then .event.payload.marker else .message end))}}'
            },
        },
        expect=200,
    )


async def _recorded(stack: TaiStack, marker: str) -> None:
    async def probe() -> bool | None:
        return True if stack.records(f"extras:{marker}") else None

    await wait_for_async(probe, deadline=20.0, message=f"no turn recorded the marker {marker!r}")


async def test_a_route_edit_on_one_replica_is_seen_by_the_next_message_everywhere(
    bridge_stack: tuple[TaiStack, str], uniq: Callable[[str], str]
) -> None:
    stack, _root = bridge_stack
    on_a, on_b = _api(bridge_stack, stack.port_a), _api(bridge_stack, stack.port_b)
    route_name = uniq("edit-route").replace("_", "-")
    execution_key = uniq("edit-exec")
    await on_b.post(
        "/api/auth/api-keys", json={"user_id": execution_key, "description": "e2e route key", "scopes": ["e2e-all"]}
    )
    user = uniq("edit-user")

    async def send(text: str, *, expect: int = 202) -> Any:
        return await on_a.post(
            f"/api/conversations/{route_name}/messages",
            json={"external_user_id": user, "text": text, "wait_seconds": 0},
            expect=expect,
        )

    # 1. Created on B, routed by A.
    await _put_route(on_b, route_name, execution_key, "created")
    first = uniq("m1")
    await send(first)
    await _recorded(stack, f"created:{first}")

    # 2. Replaced on B: the next message on A reaches the new target mapping.
    await _put_route(on_b, route_name, execution_key, "replaced")
    second = uniq("m2")
    await send(second)
    await _recorded(stack, f"replaced:{second}")
    assert stack.records(f"extras:created:{second}") == []

    # 3. The backend worker runs the event door tool and resolves the same, edited route.
    event_marker = uniq("evt")
    submitted = await on_a.post(
        "/api/tool-runs",
        json={
            "tool_name": "send_conversation_event",
            "arguments": {
                "route_name": route_name,
                "event": {"event_id": uniq("evt-id"), "kind": "e2e.probe", "payload": {"marker": event_marker}},
                "address": user,
            },
        },
        expect=202,
        retry_on_reloading=True,
    )

    async def succeeded() -> dict | None:
        view = await on_a.get(f"/api/tool-runs/{submitted['run_id']}")
        return view if view["status"] in {"succeeded", "failed"} else None

    view = await wait_for_async(succeeded, deadline=20.0, message="the backend event-door run never finished")
    assert view["status"] == "succeeded", view
    await _recorded(stack, f"replaced:{event_marker}")

    # 4. Deleted on B: the next message on A is refused with the route-resolution error.
    await on_b.delete(f"/api/conversations/{route_name}", expect=200)
    refused = await send(uniq("m3"), expect=404)
    assert route_name in str(refused), refused
