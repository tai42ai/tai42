"""L23 — the web entry gate: a capability-URL door gate on a web route.

A gated web route serves its chat page ONLY to a navigation carrying a live entry code
(``?tai_entry=…``). Codes are operator-minted through the authed management doors, multi-use,
optionally expiring, revocable, hashed at rest, and guess-throttled. The gate says "someone
holding a live code" and nothing more — it never says WHO. An ungated route is byte-identical
to today.

The security invariants pinned here: the refusal is UNIFORM — a missing, unknown, expired,
revoked or throttled code all answer the ONE byte-constant ``entry_refused`` page, so no
response differs by code validity (no oracle); the navigation guard runs BEFORE the gate, so a
non-navigation answers ``not_a_navigation`` regardless of any code it presents (again no
oracle); an existing session is an already-granted capability and is admitted without a code;
throttling is per client bucket, not a global lockout; and the management doors are authed —
a list never returns a raw code, a mint returns it exactly once, and an unauthed caller is
refused.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import redis as redis_lib

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.waiting import wait_for_async
from tai42_e2e.webchat import SESSION_COOKIE, WebChatClient, get_chat_page

from ._bridge_support import BridgeHarness

_ENTRY_THROTTLE_MATCH = "channel:web:entry_throttle:*"


def _reset_throttle(bridge: BridgeHarness) -> None:
    """Delete every entry-throttle counter on the web store. The throttle keys on the network
    client bucket — the SAME value for every request from the test process — so it is the one
    gate state that is not isolated by a per-leg ``uniq`` identity. Clearing it makes a leg's
    throttle math start from zero; the throttle keyspace is exclusively these legs'."""
    client = redis_lib.Redis.from_url(bridge.stack.resources.redis_url, decode_responses=True)
    try:
        keys = list(client.scan_iter(match=_ENTRY_THROTTLE_MATCH))
        if keys:
            client.delete(*keys)
    finally:
        client.close()


@pytest.fixture(autouse=True)
def _isolate_throttle(bridge: BridgeHarness) -> None:
    """Clear the shared entry throttle before each leg so no leg's guess volume poisons the
    next on the module stack."""
    _reset_throttle(bridge)


def _base_url(bridge: BridgeHarness) -> str:
    return f"http://{bridge.stack.host}:{bridge.stack.port_b}"


def _refusal_code(html: str) -> str | None:
    """The ``tai42-refusal-code`` meta value the page door rides its refusal code on — the
    surface these legs assert on."""
    match = re.search(r'<meta name="tai42-refusal-code" content="([^"]*)">', html)
    return match.group(1) if match else None


async def _tool_route(bridge: BridgeHarness, uniq: Callable[[str], str], tag: str, *, payload_expr: str) -> str:
    """A ``target_kind=tool`` web route recording its delivered payload; returns its identity.
    The reply maps to null so a message runs the tool (the record lands) but appends nothing."""
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    exec_key = uniq(f"{tag}-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_record",
        execution_key=exec_key,
        channel="web",
        our_identity=identity,
        payload_expr=payload_expr,
        reply_expr="null",
    )
    return identity


async def _open_gate(bridge: BridgeHarness, identity: str) -> None:
    result = await bridge.api().put(f"/api/channels/web/gates/{identity}", json={"enabled": True})
    assert result == {"enabled": True}


async def _mint_code(bridge: BridgeHarness, identity: str, *, expires_at: datetime | None = None) -> tuple[str, str]:
    """Mint one entry code through the authed door; returns ``(raw_code, code_id)``."""
    body = {"label": None, "expires_at": expires_at.isoformat() if expires_at is not None else None}
    data = await bridge.api().post(f"/api/channels/web/gates/{identity}/codes", json=body)
    assert isinstance(data["code"], str)
    assert data["code"], data
    return data["code"], data["code_id"]


async def _list_gate(bridge: BridgeHarness, identity: str) -> dict:
    return await bridge.api().get(f"/api/channels/web/gates/{identity}")


async def _wait_record(bridge: BridgeHarness, key: str, *, deadline: float = 20.0) -> list[dict]:
    async def probe() -> list[dict] | None:
        records = bridge.stack.records(key)
        return [json.loads(raw) for raw in records] if records else None

    return await wait_for_async(probe, deadline=deadline, message=f"no record under {key!r}")


async def test_an_ungated_route_is_unchanged(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    """A route with no gate serves and round-trips exactly as before — the feature is additive."""
    probe = uniq("l23a")
    identity = await _tool_route(bridge, uniq, "l23a", payload_expr=f'{{key: "{probe}", value: .message}}')
    web, page = await WebChatClient.open_page(_base_url(bridge), identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200
    text = uniq("l23a-msg")
    assert (await web.send(text)).status_code == 200
    records = await _wait_record(bridge, probe)
    assert [entry["value"] for entry in records] == [text]


async def test_a_live_code_admits_and_is_multi_use(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    """Enabling the gate + minting a code admits a navigation at ``?tai_entry=<code>`` and
    round-trips end to end; the code is multi-use — a second fresh visitor scans the SAME code
    and gets their OWN session."""
    probe = uniq("l23b")
    identity = await _tool_route(bridge, uniq, "l23b", payload_expr=f'{{key: "{probe}", value: .sender}}')
    store_url = bridge.stack.resources.redis_url
    await _open_gate(bridge, identity)
    code, _code_id = await _mint_code(bridge, identity)

    first, page = await WebChatClient.open_page(
        _base_url(bridge), identity, store_url=store_url, query={"tai_entry": code}
    )
    assert page.status_code == 200
    assert (await first.send(uniq("l23b-msg-1"))).status_code == 200

    # Multi-use: a second fresh browser scans the same code and is minted its own session.
    second, page2 = await WebChatClient.open_page(
        _base_url(bridge), identity, store_url=store_url, query={"tai_entry": code}
    )
    assert page2.status_code == 200
    assert second.visitor_id != first.visitor_id
    assert (await second.send(uniq("l23b-msg-2"))).status_code == 200

    async def both() -> list[dict] | None:
        entries = [json.loads(raw) for raw in bridge.stack.records(probe)]
        return entries if len(entries) >= 2 else None

    records = await wait_for_async(both, deadline=25.0, message="both admitted visitors did not record")
    assert {entry["value"] for entry in records} == {first.visitor_id, second.visitor_id}


async def test_a_bare_navigation_on_a_gated_route_is_refused(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    """A gated route with no ``tai_entry`` answers the 403 ``entry_refused`` page and mints no
    session cookie."""
    identity = await _tool_route(bridge, uniq, "l23c", payload_expr='{key: "x", value: .message}')
    await _open_gate(bridge, identity)
    await _mint_code(bridge, identity)

    refused = await get_chat_page(_base_url(bridge), identity)
    assert refused.status_code == 403, refused.text
    assert _refusal_code(refused.text) == "entry_refused"
    assert SESSION_COOKIE not in refused.cookies


async def test_a_wrong_code_is_refused_identically_to_a_bare_navigation(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """No oracle: an unknown code and a missing code answer the BYTE-IDENTICAL refusal page —
    a caller can never tell "wrong code" from "no code"."""
    identity = await _tool_route(bridge, uniq, "l23d", payload_expr='{key: "x", value: .message}')
    await _open_gate(bridge, identity)
    await _mint_code(bridge, identity)
    base = _base_url(bridge)

    missing = await get_chat_page(base, identity)
    wrong = await get_chat_page(base, identity, query={"tai_entry": uniq("nope")})
    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert missing.text == wrong.text, "a wrong code and a missing code must be byte-identical (no oracle)"
    # No oracle in headers or cookies either: neither refusal mints a session, and the two answers
    # differ only in the volatile Date — a code-dependent header or a wrong-code cookie would leak.
    assert SESSION_COOKIE not in missing.cookies
    assert SESSION_COOKIE not in wrong.cookies
    missing_headers = {k.lower(): v for k, v in missing.headers.items() if k.lower() != "date"}
    wrong_headers = {k.lower(): v for k, v in wrong.headers.items() if k.lower() != "date"}
    assert missing_headers == wrong_headers, "no stable header may differ by code validity (no oracle)"
    assert _refusal_code(wrong.text) == "entry_refused"


async def test_the_navigation_guard_runs_before_the_gate(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    """A non-navigation (a cross-site subresource) answers ``not_a_navigation`` regardless of
    the code it carries — the guard runs BEFORE the gate, so no response differs by code
    validity and a valid code leaks nothing to a subresource load."""
    identity = await _tool_route(bridge, uniq, "l23e", payload_expr='{key: "x", value: .message}')
    await _open_gate(bridge, identity)
    code, _code_id = await _mint_code(bridge, identity)
    base = _base_url(bridge)
    subresource = {"sec-fetch-dest": "image"}

    with_valid = await get_chat_page(base, identity, query={"tai_entry": code}, headers=subresource)
    with_none = await get_chat_page(base, identity, headers=subresource)
    assert with_valid.status_code == 403
    assert with_none.status_code == 403
    assert _refusal_code(with_valid.text) == "not_a_navigation"
    assert with_valid.text == with_none.text, "the code must not change a non-navigation's refusal"
    assert SESSION_COOKIE not in with_valid.cookies


async def test_an_expired_code_becomes_refused(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    """A short-expiry code admits while live, then answers the same 403 page once its TTL
    lapses — expiry gates NEW entries."""
    identity = await _tool_route(bridge, uniq, "l23f", payload_expr='{key: "x", value: .message}')
    store_url = bridge.stack.resources.redis_url
    await _open_gate(bridge, identity)
    code, code_id = await _mint_code(bridge, identity, expires_at=datetime.now(UTC) + timedelta(seconds=2))

    _web, page = await WebChatClient.open_page(
        _base_url(bridge), identity, store_url=store_url, query={"tai_entry": code}
    )
    assert page.status_code == 200

    # Wait for the TTL to lapse by watching the management list (which never touches the
    # throttle), so the final page GET refuses for expiry and not for a spent throttle window.
    async def gone() -> bool | None:
        listing = await _list_gate(bridge, identity)
        return True if all(entry["code_id"] != code_id for entry in listing["codes"]) else None

    await wait_for_async(gone, deadline=15.0, message="the short-expiry code never lapsed")

    refused = await get_chat_page(_base_url(bridge), identity, query={"tai_entry": code})
    assert refused.status_code == 403, refused.text
    assert _refusal_code(refused.text) == "entry_refused"


async def test_a_revoked_code_is_refused_and_the_last_revoke_leaves_the_gate_closed(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """Revoking a code refuses new entries with it; revoking the LAST code leaves the gate
    enabled but with no live code, so the route is closed (revocation is a hard cut)."""
    identity = await _tool_route(bridge, uniq, "l23g", payload_expr='{key: "x", value: .message}')
    store_url = bridge.stack.resources.redis_url
    await _open_gate(bridge, identity)
    code, code_id = await _mint_code(bridge, identity)

    _web, page = await WebChatClient.open_page(
        _base_url(bridge), identity, store_url=store_url, query={"tai_entry": code}
    )
    assert page.status_code == 200

    revoked = await bridge.api().delete(f"/api/channels/web/gates/{identity}/codes/{code_id}")
    assert revoked == {"status": "revoked"}

    listing = await _list_gate(bridge, identity)
    assert listing["enabled"] is True
    assert listing["codes"] == [], "the gate stays closed after the last revoke"

    refused = await get_chat_page(_base_url(bridge), identity, query={"tai_entry": code})
    assert refused.status_code == 403, refused.text
    assert _refusal_code(refused.text) == "entry_refused"


async def test_an_existing_session_survives_a_bare_reload(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    """A session is an already-granted capability: a visitor admitted with a valid code is
    served on a later BARE reload of the same route (no code) — the gate gates new entries,
    not established ones."""
    identity = await _tool_route(bridge, uniq, "l23h", payload_expr='{key: "x", value: .message}')
    store_url = bridge.stack.resources.redis_url
    await _open_gate(bridge, identity)
    code, _code_id = await _mint_code(bridge, identity)

    web, page = await WebChatClient.open_page(
        _base_url(bridge), identity, store_url=store_url, query={"tai_entry": code}
    )
    assert page.status_code == 200

    reload = await get_chat_page(_base_url(bridge), identity, cookies=web.cookies)
    assert reload.status_code == 200, reload.text
    assert f'data-identity="{identity}"' in reload.text, "the established session must be served the chat page"
    assert _refusal_code(reload.text) is None


async def test_rotate_gates_a_new_session_the_same_way(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    """A rotation mints a session, so a gated identity gates it: with a live code it rotates,
    without a code it answers the 403 JSON ``entry_refused`` — the rotate-door counterpart of
    the page refusal."""
    identity = await _tool_route(bridge, uniq, "l23i", payload_expr='{key: "x", value: .message}')
    store_url = bridge.stack.resources.redis_url
    await _open_gate(bridge, identity)
    code, _code_id = await _mint_code(bridge, identity)

    web, page = await WebChatClient.open_page(
        _base_url(bridge), identity, store_url=store_url, query={"tai_entry": code}
    )
    assert page.status_code == 200

    rotated, ok = await web.rotate(store_url=store_url, entry_code=code)
    assert ok.status_code == 200, ok.text
    assert rotated is not None
    assert rotated.visitor_id != web.visitor_id

    none, refused = await rotated.rotate(store_url=store_url, entry_code=None)
    assert none is None
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "entry_refused"


async def test_throttle_refuses_uniformly_and_a_clear_bucket_still_admits(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """Exhausting the guess budget refuses the NEXT attempt uniformly — even one carrying the
    VALID code (the throttle is checked before the code, so it reveals no oracle). A client
    whose throttle window is clear is then admitted with the same valid code: the throttle is a
    recoverable per-bucket gate, not a global lockout."""
    identity = await _tool_route(bridge, uniq, "l23j", payload_expr='{key: "x", value: .message}')
    base = _base_url(bridge)
    await _open_gate(bridge, identity)
    code, _code_id = await _mint_code(bridge, identity)
    # The default window cap (WebSettings.entry_attempts_per_window) unless the stack re-tuned it.
    cap = int(bridge.stack.config.env.get("CHANNEL_WEB_ENTRY_ATTEMPTS_PER_WINDOW", "10"))

    _reset_throttle(bridge)
    for _ in range(cap):
        wrong = await get_chat_page(base, identity, query={"tai_entry": uniq("nope")})
        assert wrong.status_code == 403

    # The budget is now spent: the next attempt is refused even though its code is VALID.
    throttled = await get_chat_page(base, identity, query={"tai_entry": code})
    assert throttled.status_code == 403, throttled.text
    assert _refusal_code(throttled.text) == "entry_refused"
    assert SESSION_COOKIE not in throttled.cookies

    # A client whose window is clear is admitted with that same valid code.
    _reset_throttle(bridge)
    admitted = await get_chat_page(base, identity, query={"tai_entry": code})
    assert admitted.status_code == 200, admitted.text
    assert SESSION_COOKIE in admitted.cookies


async def test_management_doors_are_authed_and_never_leak_a_raw_code(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """The management plane: a mint returns the raw code exactly once, a list returns only its
    id and metadata (never the raw code), and every door refuses an unauthed caller."""
    identity = await _tool_route(bridge, uniq, "l23k", payload_expr='{key: "x", value: .message}')
    await _open_gate(bridge, identity)
    code, code_id = await _mint_code(bridge, identity)

    listing = await _list_gate(bridge, identity)
    assert listing["enabled"] is True
    assert any(entry["code_id"] == code_id for entry in listing["codes"])
    for entry in listing["codes"]:
        assert set(entry) == {"code_id", "label", "created_at", "expires_at"}, entry
    assert code not in json.dumps(listing), "the list door must never return a raw code"

    # Every management door refuses a caller carrying no platform api key.
    anon = ApiClient(_base_url(bridge))
    read = await anon.request_raw("GET", f"/api/channels/web/gates/{identity}")
    assert read.status_code in (401, 403), read.text
    toggle = await anon.request_raw("PUT", f"/api/channels/web/gates/{identity}", json={"enabled": False})
    assert toggle.status_code in (401, 403), toggle.text
    mint = await anon.request_raw("POST", f"/api/channels/web/gates/{identity}/codes", json={"label": None})
    assert mint.status_code in (401, 403), mint.text
    revoke = await anon.request_raw("DELETE", f"/api/channels/web/gates/{identity}/codes/{code_id}")
    assert revoke.status_code in (401, 403), revoke.text


async def test_one_url_admits_and_delivers_its_link_params(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    """The combined path: one navigation carrying ``?tai_entry=<code>&x=1`` is admitted by the
    gate AND its link param reaches the tool — with ``tai_entry`` stripped from the delivered
    params (the door's own coordinate never leaks into ``.params``)."""
    probe = uniq("l23l")
    payload_expr = f'{{key: "{probe}", value: ({{x: .params.x, keys: (.params | keys)}} | tojson)}}'
    identity = await _tool_route(bridge, uniq, "l23l", payload_expr=payload_expr)
    store_url = bridge.stack.resources.redis_url
    await _open_gate(bridge, identity)
    code, _code_id = await _mint_code(bridge, identity)

    web, page = await WebChatClient.open_page(
        _base_url(bridge), identity, store_url=store_url, query={"tai_entry": code, "x": "1"}
    )
    assert page.status_code == 200
    assert (await web.send(uniq("l23l-msg"))).status_code == 200

    (entry,) = tuple(await _wait_record(bridge, probe))
    delivered = json.loads(entry["value"])
    assert delivered["x"] == "1"
    assert "tai_entry" not in delivered["keys"]
    assert delivered["keys"] == ["x"]
