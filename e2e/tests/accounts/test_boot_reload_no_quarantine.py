"""C-accounts — the accounts distribution's dual-role wiring boots and reloads clean.

``accounts-postgres`` is wired under TWO manifest roles at once: its ROOT package sits
under ``lifecycle_modules`` while its ``routes_login`` / ``routes_users`` submodules sit
under ``routers_modules``. The boot package walk binds each mapped route submodule to its
OWN mount and runs each module body once per pass, so the distribution never quarantines
when its route submodules are swept into the lifecycle role's walk. A bare-mount import
would raise ``MountRegistrationError`` and — because accounts-postgres is the configured
auth provider — abort the whole boot, so the load-bearing regression signal here is the
stack COMING UP at all; the zero-quarantine reads then pin the survivable-plugin variant
of the same class. This drives that shape
over the real stack: a boot and a fleet reload each leave ZERO plugins quarantined while
both route families keep serving — login under ``/api/login``, users under ``/api/auth``.
"""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

_PASSWORD = "correct-horse-battery-staple"


async def _assert_no_quarantine(stack: TaiStack, port: int) -> None:
    """Readiness on one replica: 200 and ZERO plugins parked by this worker's boot pass.
    ``/ready`` carries ``plugin_quarantine`` — the count the boot walk quarantined; the
    dual-role accounts wiring must never land there. Read with the seeded root key: under
    access control this stack fences ``/ready`` (only ``/health`` is pinned public)."""
    resp = await stack.api(port=port).request_raw("GET", "/ready")
    assert resp.status_code == 200, f"readiness on :{port} -> {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["plugin_quarantine"] == 0, f"boot quarantined a plugin on :{port}: {body}"


async def test_dual_role_accounts_boots_and_reloads_without_quarantine(
    accounts_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = accounts_stack
    token = stack.config.env["TAI_ACCOUNTS_BOOTSTRAP_TOKEN"]
    admin = stack.api(port=stack.port_a)  # seeded root sk- key
    public_a = ApiClient(f"http://{stack.host}:{stack.port_a}")

    # Boot health: neither replica quarantined the accounts distribution despite its root
    # riding lifecycle_modules while its route submodules ride routers_modules.
    await _assert_no_quarantine(stack, stack.port_a)
    await _assert_no_quarantine(stack, stack.port_b)

    # routes_login serves: bootstrap the first owner, then a real password round-trip mints
    # a session — the login family is mounted under /api/login, not stranded by quarantine.
    owner_email = f"{uniq('owner')}@e2e.test"
    await public_a.post(
        "/api/login/bootstrap",
        json={"email": owner_email, "password": _PASSWORD, "bootstrap_token": token},
        retry_on_reloading=True,
    )
    login = await public_a.post("/api/login/password", json={"email": owner_email, "password": _PASSWORD})
    session = login["token"]
    assert session.startswith("tai-sess-"), f"login must mint a session token: {session[:12]!r}"

    # routes_users serves: the owner session reads the authed user list under /api/auth, so
    # the session minted by routes_login authorizes a read on the sibling routes_users mount.
    owner_client = stack.api(port=stack.port_b).with_token(session)
    listed = await owner_client.get("/api/auth/users")
    assert any(u["email"] == owner_email for u in listed["users"]), listed

    # A fleet reload re-imports every manifest module under its binding on every worker: the
    # dual-role distribution must re-fire each route submodule once and stay off quarantine.
    report = await admin.post("/api/config/reload", json={}, retry_on_reloading=True, timeout=60.0)
    assert report["reachable"] is True, f"the reload broadcast was unreachable: {report}"
    outcomes = {r["name"]: r["outcome"] for r in report["results"]}
    census = {worker.name for worker in stack.census()}
    assert census <= outcomes.keys(), f"a census worker missed the reload report: census={census} report={report}"
    assert all(outcomes[name] == "applied" for name in census), f"a worker did not apply the reload: {report}"

    # Post-reload boot health: still zero quarantines on both replicas.
    await _assert_no_quarantine(stack, stack.port_a)
    await _assert_no_quarantine(stack, stack.port_b)

    # Both route families still serve after the reload: a fresh password login and an authed
    # users read both succeed against the re-imported route table.
    relogin = await public_a.post(
        "/api/login/password", json={"email": owner_email, "password": _PASSWORD}, retry_on_reloading=True
    )
    reread = (
        await stack.api(port=stack.port_b).with_token(relogin["token"]).get("/api/auth/users", retry_on_reloading=True)
    )
    assert any(u["email"] == owner_email for u in reread["users"]), reread
