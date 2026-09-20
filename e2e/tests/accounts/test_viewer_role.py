"""C-accounts — the viewer role is read-only through its jq method condition.

A viewer session may issue read-only requests but is denied any state-changing POST
outside the self-service surfaces: the seeded viewer jq condition gates on the
request method, so a tool-run submission is refused while a listing GET succeeds."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

_PASSWORD = "viewer-user-password-1"


async def test_viewer_session_get_ok_post_denied(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)  # seeded root sk- key

    # A viewer account with a live policy, and a session for it (accept the invite).
    created = await admin.post("/api/auth/users", json={"email": f"{uniq('viewer')}@e2e.test", "role": "viewer"})
    public = ApiClient(f"http://{stack.host}:{stack.port_a}")
    accepted = await public.post(
        "/api/login/invite/accept",
        json={"invite_token": created["invite_token"], "password": _PASSWORD, "password_confirm": _PASSWORD},
    )
    viewer = stack.api(port=stack.port_b).with_token(accepted["token"])

    # Read-only GETs succeed for a viewer.
    kinds = await viewer.get("/api/system/kinds")
    assert any(row["kind"] == "accounts" for row in kinds), kinds
    runs = await viewer.request_raw("GET", "/api/tool-runs?tool_name=e2e_echo")
    assert runs.status_code == 200, f"a viewer must read the tool-runs listing: {runs.status_code} {runs.text}"

    # A state-changing POST (submitting a tool run) is denied by the viewer jq
    # method condition — never reaching the handler.
    denied = await viewer.request_raw(
        "POST", "/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "x"}}
    )
    assert denied.status_code == 403, f"a viewer must be denied a tool-run POST: {denied.status_code} {denied.text}"
