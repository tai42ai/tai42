"""The entry-gate management doors (authed) — the pinned action-classes, gate
read/toggle, code mint/list/revoke, and the unusable-identity refusal."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.store.entry_gate import mint_entry_code, set_gate

from .conftest import IDENTITY, FakeRedis, _body, build_request

pytestmark = pytest.mark.usefixtures("web_env")

# The RELATIVE registered paths; the gate doors read the identity from the path
# params, not the request URL, so a relative scope path drives them fine.
_GATES = "/gates/{identity}"
_CODES = "/gates/{identity}/codes"
_CODE = "/gates/{identity}/codes/{code_id}"


def _managed_route(stub_app, path: str, method: str):
    routes = [route for route in stub_app.http.routes if route.path == path and method in route.methods]
    assert len(routes) == 1
    return routes[0]


def _gate_request(method: str, path: str, *, json_body=None, path_params: dict[str, str]):
    return build_request(method=method, path=path, json_body=json_body, path_params=path_params)


def test_management_doors_are_authed_with_the_pinned_action_class(stub_app):
    # The module passes no explicit ``authed`` — the runtime resolves it from the
    # declaration's ``public: false``. Each door still declares its action-class (an
    # authed route with none refuses to register); the stub records that pinned class.
    read = _managed_route(stub_app, _GATES, "GET")
    assert read.authed is None
    assert read.action == "read"
    for path, method in [(_GATES, "PUT"), (_CODES, "POST"), (_CODE, "DELETE")]:
        route = _managed_route(stub_app, path, method)
        assert route.authed is None
        assert route.action == "write"


async def test_gate_read_returns_the_flag_and_its_codes(web_env, stub_app, fake_redis: FakeRedis):
    await set_gate(IDENTITY, True)
    _, code_id = await mint_entry_code(IDENTITY, "spring", None)
    resp = await _managed_route(stub_app, _GATES, "GET").handler(
        _gate_request("GET", _GATES, path_params={"identity": IDENTITY})
    )
    data = _body(resp)["data"]
    assert data["enabled"] is True
    assert [code["code_id"] for code in data["codes"]] == [code_id]
    assert data["codes"][0]["label"] == "spring"


async def test_gate_toggle_sets_the_explicit_flag(web_env, stub_app, fake_redis: FakeRedis):
    route = _managed_route(stub_app, _GATES, "PUT")
    on = await route.handler(
        _gate_request("PUT", _GATES, json_body={"enabled": True}, path_params={"identity": IDENTITY})
    )
    assert _body(on)["data"] == {"enabled": True}
    assert f"channel:web:entry_gate:{IDENTITY}" in fake_redis.store
    off = await route.handler(
        _gate_request("PUT", _GATES, json_body={"enabled": False}, path_params={"identity": IDENTITY})
    )
    assert _body(off)["data"] == {"enabled": False}
    assert f"channel:web:entry_gate:{IDENTITY}" not in fake_redis.store


async def test_mint_returns_the_raw_code_once_and_list_never_does(web_env, stub_app, fake_redis: FakeRedis):
    minted = await _managed_route(stub_app, _CODES, "POST").handler(
        _gate_request(
            "POST", _CODES, json_body={"label": "launch", "expires_at": None}, path_params={"identity": IDENTITY}
        )
    )
    data = _body(minted)["data"]
    raw_code = data["code"]
    assert data["code_id"] == hashlib.sha256(raw_code.encode()).hexdigest()
    listed = _body(
        await _managed_route(stub_app, _GATES, "GET").handler(
            _gate_request("GET", _GATES, path_params={"identity": IDENTITY})
        )
    )["data"]
    # The raw code was returned ONCE at mint and is never read back.
    assert raw_code not in json.dumps(listed)
    assert [code["code_id"] for code in listed["codes"]] == [data["code_id"]]


async def test_mint_with_an_expiry_returns_it(web_env, stub_app, fake_redis: FakeRedis):
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    resp = await _managed_route(stub_app, _CODES, "POST").handler(
        _gate_request(
            "POST", _CODES, json_body={"label": None, "expires_at": expires_at}, path_params={"identity": IDENTITY}
        )
    )
    assert _body(resp)["data"]["expires_at"] == expires_at


@pytest.mark.parametrize(
    "expires_at",
    [
        "2000-01-01T00:00:00+00:00",  # in the past
        "2999-01-01T00:00:00",  # naive (no timezone)
    ],
)
async def test_mint_refuses_a_past_or_naive_expiry(web_env, stub_app, fake_redis: FakeRedis, expires_at: str):
    resp = await _managed_route(stub_app, _CODES, "POST").handler(
        _gate_request("POST", _CODES, json_body={"expires_at": expires_at}, path_params={"identity": IDENTITY})
    )
    assert resp.status_code == 422
    assert "expires_at" in _body(resp)["error"]


async def test_revoke_kills_the_code_and_404s_an_unknown_id(web_env, stub_app, fake_redis: FakeRedis):
    _, code_id = await mint_entry_code(IDENTITY, None, None)
    route = _managed_route(stub_app, _CODE, "DELETE")
    ok = await route.handler(_gate_request("DELETE", _CODE, path_params={"identity": IDENTITY, "code_id": code_id}))
    assert _body(ok)["data"] == {"status": "revoked"}
    gone = await route.handler(_gate_request("DELETE", _CODE, path_params={"identity": IDENTITY, "code_id": code_id}))
    assert gone.status_code == 404


async def test_management_doors_refuse_an_unusable_identity(web_env, stub_app):
    resp = await _managed_route(stub_app, _GATES, "GET").handler(
        _gate_request("GET", _GATES, path_params={"identity": "site:alpha"})
    )
    assert resp.status_code == 422
