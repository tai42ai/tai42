"""C7 — backend identity plus the bus-backed fleet doors: identity, the live worker
census, the fleet reload-config op (all workers + a bogus target), the admin fence on
the reload door, and the honest busless answers on a stack that registers no backend.

The census and reload doors read and mutate the app-owned worker bus, so they need no
backend at all — a busless bare stack still lists its own local worker and reloads it
locally (``local_only``), so the absent-backend answers SUCCEED (200, ``local_only``):
the census lists the lone local worker and the reload applies locally. The reload door
is admin-only; the census door is unfenced."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from tai42_e2e import wait_for_async
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack, _probe_tolerating_reloading

_PASSWORD = "fleet-fence-password-1"


async def _reload(api: ApiClient, targets: list[str] | None) -> httpx.Response:
    """POST the fleet reload door, polling past the boot-time reload gate (a held gate
    answers a retriable 503) so the returned response is the door's real verdict."""

    async def settled() -> httpx.Response | None:
        resp = await api.request_raw("POST", "/api/fleet/reload-config", json={"targets": targets})
        return resp if resp.status_code != 503 else None

    return await wait_for_async(settled, deadline=10.0, message="fleet reload never left the reload gate")


async def test_backend_identity(core_stack: TaiStack) -> None:
    # The door reports ``type(backend).__name__`` / ``__module__``; each backend
    # variant owns that identity, exactly as the storage variant owns the
    # ``/api/storage`` door's.
    backend = core_stack.infra.variants.backend
    api = core_stack.api()
    info = await api.get("/api/backend")
    assert info == {"present": True, "backend": backend.provider_class, "module": backend.provider_module}


async def test_fleet_workers_lists_the_live_fleet(core_stack: TaiStack) -> None:
    api = core_stack.api()
    workers = (await api.get("/api/fleet/workers"))["workers"]
    assert workers, "the fleet-backed stack reported an empty worker fleet"
    # Each worker is an origin OBJECT — identity, kind, and pid — not a bare name.
    for worker in workers:
        assert worker.keys() >= {"origin", "kind", "pid"}, f"worker missing origin fields: {worker}"
        assert worker["origin"], f"worker carries a blank origin: {worker}"
        assert worker["kind"] in {"serve", "backend"}, f"worker carries an unknown kind: {worker}"
        assert isinstance(worker["pid"], int), f"worker pid is not an int: {worker}"
    # The door's census is exactly the bus presence census the harness scanned at boot.
    assert {w["origin"] for w in workers} == {origin.origin for origin in core_stack.census()}


async def test_fleet_reload_config_all_confirms(core_stack: TaiStack) -> None:
    api = core_stack.api()
    resp = await _reload(api, None)
    assert resp.status_code == 200, resp.text
    result = resp.json()["data"]
    # A bare FleetResult (no ``reloaded`` key): reachable, one per-origin outcome each.
    assert result["op"] == "reload_config"
    assert result["reachable"] is True
    outcomes = {r["origin"]: r["outcome"] for r in result["results"]}
    assert all(outcome == "applied" for outcome in outcomes.values()), f"an origin did not apply: {result}"
    # Every live census origin (the backend worker + each HTTP worker) confirmed.
    assert {origin.origin for origin in core_stack.census()} <= outcomes.keys()


async def test_fleet_reload_config_bogus_target_raises_naming_it(core_stack: TaiStack) -> None:
    bogus = "no-such-worker-99999"
    api = core_stack.api()
    # The op validates targets against the bus census BEFORE any side effect; an
    # unknown target is rejected as a client error — a 400, never a silent narrowing
    # to the live fleet — and the door's body NAMES the unknown target.
    resp = await _reload(api, [bogus])
    assert resp.status_code == 400, resp.text
    assert bogus in resp.text, resp.text

    # The validate-targets raise NAMES the unknown worker on the HTTP door AND, verbatim,
    # on the fleet ``reload_config`` tool.
    async def _call_bogus_tool():
        async with core_stack.mcp() as mcp:
            return await mcp.call_tool(
                "reload_config", {"targets": [bogus]}, raise_on_error=False, retry_on_reloading=True
            )

    # The prior test's fleet reload swaps the epoch; its deferred session-manager close
    # can retire a freshly-opened MCP session mid-call (D13a: a swap terminates sessions).
    # Re-open and re-issue on a fresh session until it lands cleanly (past the swap window
    # and the boot reload gate) — exactly as a real client re-initialises.
    result = await wait_for_async(
        lambda: _probe_tolerating_reloading(_call_bogus_tool),
        deadline=10.0,
        message="the bogus reload_config tool never left the reload/session-swap window",
    )
    assert result.is_error, f"a bogus target must fail the tool loudly: {result}"
    assert bogus in " ".join(getattr(part, "text", "") for part in result.content), result


async def test_backend_absent_is_honest(bare_stack: TaiStack) -> None:
    api = bare_stack.api()

    # The identity door never errors — it reports the absence truthfully.
    info = await api.get("/api/backend")
    assert info == {"present": False, "backend": None, "module": None}

    # The bus-backed doors SUCCEED busless: the census lists this worker's own local
    # origin and the reload applies to it locally (``local_only``) — no backend needed.
    workers = (await api.get("/api/fleet/workers"))["workers"]
    assert workers, "the busless stack reported an empty local worker fleet"
    assert all(worker["kind"] == "serve" and worker["origin"] for worker in workers), workers

    resp = await _reload(api, None)
    assert resp.status_code == 200, resp.text
    result = resp.json()["data"]
    assert result["op"] == "reload_config"
    assert result["reachable"] is True
    assert result["local_only"] is True, f"a busless reload must be local-only: {result}"
    assert [r["outcome"] for r in result["results"]] == ["applied"], f"the local worker did not apply: {result}"


async def test_reload_door_is_admin_fenced_census_is_open(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """The fleet reload door is admin-only — a recovery/ops action — so editor and
    viewer are denied it; the census door stays open to any authenticated role."""
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)  # seeded root sk- key

    async def session_for(role: str) -> ApiClient:
        created = await admin.post("/api/auth/users", json={"email": f"{uniq(role)}@e2e.test", "role": role})
        public = ApiClient(f"http://{stack.host}:{stack.port_a}")
        accepted = await public.post(
            "/api/login/invite/accept",
            json={"invite_token": created["invite_token"], "password": _PASSWORD, "password_confirm": _PASSWORD},
        )
        return stack.api(port=stack.port_b).with_token(accepted["token"])

    editor = await session_for("editor")
    viewer = await session_for("viewer")

    # The reload door is fenced: editor AND viewer are denied regardless of method —
    # the fence is on the route, so the 403 lands before the reload gate is consulted.
    for role, session in (("editor", editor), ("viewer", viewer)):
        denied = await session.request_raw("POST", "/api/fleet/reload-config", json={"targets": None})
        assert denied.status_code == 403, f"{role} not denied the fleet reload: {denied.status_code} {denied.text}"

    # The census door is unfenced: a non-admin reads the live worker fleet.
    workers = await viewer.request_raw("GET", "/api/fleet/workers")
    assert workers.status_code == 200, f"a viewer must read the fleet census: {workers.status_code} {workers.text}"
    assert workers.json()["data"]["workers"], "the census door returned an empty fleet to the viewer"
