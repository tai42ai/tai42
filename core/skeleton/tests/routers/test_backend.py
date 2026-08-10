"""Backend identity + the fleet doors: identity, the bus-backed worker census, and
the fleet soft-restart.

Handlers are driven directly (the router-test pattern); the ``tai42_app.backends``
facet is faked by swapping the bound app impl, and the worker bus is a
:class:`FakeBus` set on the concrete app singleton. ``list_workers`` returns the bus
census (no backend needed) and a census read that raises must propagate (a read that
cannot read fails loudly). The reload door applies locally then broadcasts.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.requests import Request
from tai42_contract.app import tai42_app

from tai42_skeleton.app import instance
from tai42_skeleton.routers import backend as router
from tests._fakes.bus import FakeBus


class _FakeBackend:
    def __init__(self) -> None:
        self.reload_calls: list = []

    async def launch(self, args) -> None:
        return None


class _Admin:
    def __init__(self) -> None:
        self.reload_calls = 0

    def reload_config(self) -> dict:
        self.reload_calls += 1
        return {"status": "ok"}


@pytest.fixture
def install(monkeypatch):
    def _install(*, backend=None, admin=None, bus: FakeBus | None = None) -> FakeBus:
        monkeypatch.setattr(
            tai42_app, "_impl", SimpleNamespace(backends=SimpleNamespace(backend=backend), admin=admin or _Admin())
        )
        bus = bus or FakeBus()
        monkeypatch.setattr(instance.app, "_bus", bus)
        return bus

    return _install


def _req(**path_params) -> Request:
    return cast(Request, SimpleNamespace(path_params=path_params))


def _body_req(body: bytes) -> Request:
    scope = {"type": "http", "method": "POST", "path": "/api/fleet/reload-config", "headers": [], "query_string": b""}
    delivered = {"done": False}

    async def receive():
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _json(resp) -> dict:
    return json.loads(bytes(resp.body))


# -- identity ----------------------------------------------------------------


async def test_info_present(install):
    fake = _FakeBackend()
    install(backend=fake)
    resp = await router.backend_info(_req())
    assert resp.status_code == 200
    data = _json(resp)["data"]
    assert data["present"] is True
    assert data["backend"] == "_FakeBackend"
    assert data["module"] == type(fake).__module__


async def test_info_absent_is_200_present_false(install):
    install(backend=None)
    resp = await router.backend_info(_req())
    assert resp.status_code == 200
    assert _json(resp) == {"data": {"present": False, "backend": None, "module": None}}


# -- workers (bus census) ----------------------------------------------------


async def test_workers_lists_census(install):
    install(bus=FakeBus(origin="serve-a", remotes=["backend-b"]))
    resp = await router.list_workers(_req())
    assert resp.status_code == 200
    origins = {w["name"] for w in _json(resp)["data"]["workers"]}
    assert origins == {"serve-a", "backend-b"}


async def test_workers_no_backend_still_lists(install):
    # The census is bus-backed, so it lists this worker even with no task backend.
    install(backend=None, bus=FakeBus(origin="serve-solo"))
    resp = await router.list_workers(_req())
    assert resp.status_code == 200
    assert [w["name"] for w in _json(resp)["data"]["workers"]] == ["serve-solo"]


async def test_workers_reports_server_computed_stale_flag(install):
    # ``stale`` is computed server-side from the SAME freshness predicate the ready+fresh expected set uses
    # — a fresh row is stale=False, a row whose captured PTTL has decayed is stale=True.
    # The client never hardcodes a threshold; it reads the flag.
    bus = install(bus=FakeBus(origin="serve-a", remotes=["backend-b"]))
    # Decay the remote row's captured PTTL below the freshness bound (ttl=15 → bound=5s).
    bus.rows[1].pttl_ms = 1000
    resp = await router.list_workers(_req())
    assert resp.status_code == 200
    by_name = {w["name"]: w for w in _json(resp)["data"]["workers"]}
    assert by_name["serve-a"]["stale"] is False
    assert by_name["backend-b"]["stale"] is True
    # The row carries the full identity/timestamp fields plus generation and state.
    assert by_name["serve-a"]["generation"] == 1
    assert by_name["serve-a"]["state"] == "ready"
    assert by_name["serve-a"]["last_op"] is None


async def test_busless_worker_reports_not_stale(install, monkeypatch):
    # A GENUINELY busless bus (WorkerBus.local, not FakeBus) synthesizes its one live row
    # fresh — its captured PTTL clears the freshness bound — so the listing reports
    # stale=False for the lone worker. Regression: reverting _local_row's pttl_ms to None
    # makes presence_fresh(None) False and the busless worker falsely report stale=True.
    from tai42_skeleton.app.bus import WorkerBus

    install()
    monkeypatch.setattr(instance.app, "_bus", WorkerBus.local())
    resp = await router.list_workers(_req())
    assert resp.status_code == 200
    workers = _json(resp)["data"]["workers"]
    assert len(workers) == 1
    assert workers[0]["stale"] is False


async def test_workers_census_failure_propagates(install):
    class _BrokenBus(FakeBus):
        async def census(self):
            raise RuntimeError("presence store down")

    install(bus=_BrokenBus())
    with pytest.raises(RuntimeError, match="presence store down"):
        await router.list_workers(_req())


# -- reload-config (fleet soft-restart) --------------------------------------


async def test_reload_happy_all_workers(install):
    admin = _Admin()
    install(admin=admin, bus=FakeBus(origin="serve-a"))
    resp = await router.reload_config(_body_req(b'{"targets": null}'))
    assert resp.status_code == 200
    data = _json(resp)["data"]
    assert data["op"] == "reload_config"
    # The serving worker applied its own reload first (its self entry).
    assert admin.reload_calls == 1
    assert data["results"][0]["name"] == "serve-a"
    assert data["results"][0]["outcome"] == "applied"


async def test_reload_targets_a_named_worker(install):
    admin = _Admin()
    install(admin=admin, bus=FakeBus(origin="serve-a", remotes=["backend-b"]))
    resp = await router.reload_config(_body_req(b'{"targets": ["backend-b"]}'))
    assert resp.status_code == 200
    data = _json(resp)["data"]
    # Targets exclude the serving worker → it does not reload itself.
    assert admin.reload_calls == 0
    assert {r["name"] for r in data["results"]} == {"backend-b"}


async def test_reload_bad_json_400(install):
    install()
    resp = await router.reload_config(_body_req(b"not json"))
    assert resp.status_code == 400


async def test_reload_bad_targets_400(install):
    install()
    resp = await router.reload_config(_body_req(b'{"targets": [1, 2]}'))
    assert resp.status_code == 400
    assert "targets" in _json(resp)["error"]


async def test_reload_unknown_target_400_names_target(install):
    # A well-typed but bogus worker name (absent from the census) is a caller
    # mistake: the bus rejects it and the door surfaces a named 400 that NAMES the
    # unknown target — never a bare 500. This pins the contract the e2e relies on.
    admin = _Admin()
    install(admin=admin, bus=FakeBus(origin="serve-a"))
    resp = await router.reload_config(_body_req(b'{"targets": ["ghost"]}'))
    assert resp.status_code == 400
    assert "ghost" in _json(resp)["error"]
    # Validation precedes side effects: nothing reloaded locally.
    assert admin.reload_calls == 0


# -- CLI collision avoidance -------------------------------------------------


def test_tai_backend_still_resolves_to_launcher():
    # The ``fleet`` group name (not ``backend``) exists so the runtime launcher
    # mounted as ``tai backend`` is not clobbered — assert both stand.
    from tai42_cli.app import app

    from tai42_skeleton.cli import backend as backend_launcher

    assert app.commands["backend"] is backend_launcher.main
    fleet_subcommands = getattr(app.commands["fleet"], "commands", None)
    assert fleet_subcommands is not None
    assert set(fleet_subcommands) == {"info", "workers", "reload-config"}
