"""The api door admits a turn with access control OFF.

With no auth adapter the request carries no caller id, yet the api door must still run the
turn — acting AS the platform's synthetic ``__no_auth__`` principal, which keys the thread
and the rate bucket exactly as a real caller's id would — rather than refusing it 501 for
want of an authenticated caller. The composed path is the reporter's: create an api-door
route, POST a message with no ``Authorization`` header, and read the accepted turn back.
"""

from __future__ import annotations

from tai42_e2e.stack import TaiStack

# An https callback with no receiver: the async delivery connection-refuses, which never
# touches the door's ``202`` admission the spec asserts.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


async def test_ac_off_api_door_admits_and_keys_by_the_synthetic_principal(
    conversations_off_stack: TaiStack,
) -> None:
    api = conversations_off_stack.api()  # no auth token: access control is OFF
    route_name = "acoff-uuid-route"
    created = await api.post(
        f"/api/conversations/{route_name}",
        json={
            "door": "api",
            "target_kind": "tool",
            "target_name": "generate_uuid",
            "execution_key": "acoff-exec",
            "callback_url": _UNREACHABLE_CALLBACK,
        },
        retry_on_reloading=True,
    )
    assert created["created"] is True

    # No Authorization header: the door resolves the synthetic no-auth principal and admits
    # the turn (202), rather than refusing it 501 for want of an authenticated caller.
    data = await api.post(
        f"/api/conversations/{route_name}/messages",
        json={"external_user_id": "u1", "text": "hello"},
        expect=202,
    )
    # The thread is keyed by the synthetic principal — the door bucketed the caller by it.
    assert "__no_auth__" in data["thread_id"]
