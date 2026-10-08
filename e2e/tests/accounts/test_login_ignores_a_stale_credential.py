"""C-accounts — the login routes ignore a stale credential the caller still presents.

Every login route declares itself a pre-authentication route, so a credential presented on
it is never verified: a browser holding an expired session or a revoked key can still sign
in instead of being refused by the very door that replaces the credential. Anywhere else the
same stale credential is refused.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.accounts_flow import invite_accept_login
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:identity", "kind:accounts:postgres")

_PASSWORD = "stale-credential-password-1"


async def test_password_login_ignores_a_stale_bearer(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)
    public = ApiClient(stack.origin(stack.port_a))
    email = f"{uniq('stale')}@e2e.test"
    await invite_accept_login(admin, public, email=email, role="viewer", password=_PASSWORD)

    stale = ApiClient(stack.origin(stack.port_a)).with_token("sk-stale")

    # Non-vacuous: the stale bearer is refused on an authenticated route.
    refused = await stale.request_raw("GET", "/api/auth/me")
    assert refused.status_code == 401, f"a stale bearer must 401 off the login routes: {refused.status_code}"

    # The accounts plugin's password login ignores it and signs the caller in.
    login = await stale.request_raw("POST", "/api/login/password", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200, f"the login route must ignore a stale bearer: {login.status_code} {login.text}"
    assert login.json()["data"]["token"].startswith("tai-sess-"), login.text

    # The platform's own login methods door ignores it too.
    methods = await stale.request_raw("GET", "/api/login/methods")
    assert methods.status_code == 200, f"the methods door must ignore a stale bearer: {methods.status_code}"
