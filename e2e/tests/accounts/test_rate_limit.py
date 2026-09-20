"""C-accounts — login throttling is failures-only with a Retry-After signal.

Repeated wrong passwords for one account trip the per-account backoff: the attempt
after the threshold is refused with a 429 carrying a Retry-After header. A correct
password is never blocked by the throttle (failures-only), so it succeeds even
while the account's failure counter is hot."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

_PASSWORD = "rate-limit-user-password-1"


async def test_wrong_passwords_throttle_then_correct_succeeds(
    accounts_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)
    public = ApiClient(f"http://{stack.host}:{stack.port_a}")

    # An account with a known password (set through an accepted invite).
    email = f"{uniq('user')}@e2e.test"
    created = await admin.post("/api/auth/users", json={"email": email, "role": "editor"})
    await public.post(
        "/api/login/invite/accept",
        json={"invite_token": created["invite_token"], "password": _PASSWORD, "password_confirm": _PASSWORD},
    )

    # Hammer the account with wrong passwords until the backoff trips. The per-account
    # threshold is small, so a bounded loop reaches the 429 well before the per-IP cap.
    throttled = None
    seen_401 = False
    for _ in range(12):
        response = await public.request_raw(
            "POST", "/api/login/password", json={"email": email, "password": "definitely-wrong"}
        )
        if response.status_code == 401:
            seen_401 = True
            continue
        if response.status_code == 429:
            throttled = response
            break
        raise AssertionError(f"unexpected login status {response.status_code}: {response.text}")

    assert seen_401, "the first wrong attempts must be a plain 401 before the throttle engages"
    assert throttled is not None, "repeated wrong passwords never produced a 429"
    assert throttled.headers.get("Retry-After"), f"the 429 must carry Retry-After: {dict(throttled.headers)}"
    assert int(throttled.headers["Retry-After"]) > 0, throttled.headers["Retry-After"]

    # Failures-only: the correct password succeeds even with the failure counter hot —
    # an attacker who knows the email cannot lock the real owner out.
    login = await public.post("/api/login/password", json={"email": email, "password": _PASSWORD})
    assert login["token"].startswith("tai-sess-"), f"the correct password must still succeed: {login}"
