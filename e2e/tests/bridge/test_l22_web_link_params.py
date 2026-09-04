"""L22 — link params: query parameters carried onto a tool target's payload.

The platform is a DUMB transport for link params. A query parameter on the chat-page URL
(anything but the door's own ``tai_pair`` / ``tai_entry`` coordinates) is captured with the
visitor's session and delivered to the turn's tool payload under its OWN ``params`` key —
never merged into the root, never interpreted, never trusted. The web page door captures
them; the authed API door takes the same field on its message body, for one uniform
tool-payload contract. This leg drives both doors against a ``target_kind=tool`` route whose
``payload_expr`` reads ``.params.*`` and records what the tool actually received, so the
assertion is on the delivered payload and not on any intermediate.

The security invariants pinned here: the payload ``sender`` is the server-side visitor id
and is NOT spoofable by a ``?sender=`` param (a caller value lands only under
``.params.sender``); ``tai_pair`` and ``tai_entry`` never reach the delivered params; a bounds
violation is a byte-constant refusal PAGE (``link_params_invalid`` in the meta, no session
minted); and the API door's invalid-params refusal names the violated bound WITHOUT echoing
a value.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tai42_e2e.waiting import wait_for_async
from tai42_e2e.webchat import SESSION_COOKIE, WebChatClient, get_chat_page

from ._bridge_support import BridgeHarness, wait_probe_record

# An https callback with no server behind it — the api-door tool legs map their reply to null
# and wait inline, so the callback is suppressed and never dialled; it names no reachable host.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


def _record_expr(key_expr: str, value_expr: str) -> str:
    """A jq program mapping the turn payload to ``e2e_record(key, value)`` kwargs — ``key`` and
    ``value`` are jq snippets (a quoted literal, or a path like ``.message``)."""
    return f"{{key: {key_expr}, value: {value_expr}}}"


def _base_url(bridge: BridgeHarness) -> str:
    return f"http://{bridge.stack.host}:{bridge.stack.port_b}"


async def _web_tool_route(bridge: BridgeHarness, uniq: Callable[[str], str], tag: str, *, payload_expr: str) -> str:
    """Create a ``target_kind=tool`` web route whose tool records the delivered payload, and
    return its identity. The reply maps to null so the turn runs (the record side effect
    lands) but nothing is appended to the visitor transcript."""
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


async def _wait_record_count(bridge: BridgeHarness, key: str, count: int, *, deadline: float = 20.0) -> list[dict]:
    """Wait until the ``e2e_record`` list under ``key`` holds ``count`` entries; assert it
    settled at exactly ``count`` and return them decoded."""

    async def probe() -> list[dict] | None:
        records = bridge.stack.records(key)
        return [json.loads(raw) for raw in records] if len(records) >= count else None

    entries = await wait_for_async(probe, deadline=deadline, message=f"fewer than {count} records under {key!r}")
    assert len(entries) == count, f"expected exactly {count} records under {key!r}, saw {len(entries)}"
    return entries


async def test_link_params_reach_the_tool_and_sender_is_the_unspoofable_visitor_id(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """A navigation's params reach ``.params`` on the tool payload; the root ``sender`` stays
    the server-side visitor id even when the URL carries ``?sender=`` (that value lands only
    under ``.params.sender``, never over the address the platform attests)."""
    probe = uniq("l22-sender")
    value_expr = "({x: .params.x, y: .params.y, sender: .sender, spoofed: .params.sender} | tojson)"
    identity = await _web_tool_route(bridge, uniq, "l22a", payload_expr=_record_expr(json.dumps(probe), value_expr))

    web, _page = await WebChatClient.open_page(
        _base_url(bridge),
        identity,
        store_url=bridge.stack.resources.redis_url,
        query={"x": "1", "y": "2", "sender": "fake"},
    )
    assert (await web.send(uniq("l22a-msg"))).status_code == 200

    (entry,) = await _wait_record_count(bridge, probe, 1)
    payload = json.loads(entry["value"])
    assert payload["x"] == "1"
    assert payload["y"] == "2"
    # The tool's ``sender`` is the visitor id the server registered, never the caller's
    # ``?sender=`` — which is delivered, but only under ``.params``.
    assert payload["sender"] == web.visitor_id
    assert payload["spoofed"] == "fake"


async def test_params_persist_across_every_message_of_the_session(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """The params captured at the entry ride EVERY message of that session, not just the
    first — the tool sees them on message two and three exactly as on message one."""
    value = uniq("l22-persist-val")
    payload_expr = _record_expr(".message", '(.params.x // "none")')
    identity = await _web_tool_route(bridge, uniq, "l22b", payload_expr=payload_expr)

    web, _page = await WebChatClient.open_page(
        _base_url(bridge), identity, store_url=bridge.stack.resources.redis_url, query={"x": value}
    )
    texts = [uniq(f"l22b-msg-{i}") for i in range(3)]
    for text in texts:
        assert (await web.send(text)).status_code == 200

    for text in texts:
        (entry,) = await _wait_record_count(bridge, text, 1)
        assert entry["value"] == value, "a later message of the same session dropped the captured params"


async def test_renavigation_replaces_params_and_a_bare_renavigation_keeps_them(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """Re-opening the chat page on the SAME session with new params rewrites them; re-opening
    with NO params leaves the stored ones untouched (same token, same visitor id throughout)."""
    payload_expr = _record_expr(".message", '(.params.x // "none")')
    identity = await _web_tool_route(bridge, uniq, "l22c", payload_expr=payload_expr)
    store_url = bridge.stack.resources.redis_url

    first = uniq("l22c-first")
    web, _page = await WebChatClient.open_page(_base_url(bridge), identity, store_url=store_url, query={"x": first})
    text1 = uniq("l22c-msg-1")
    assert (await web.send(text1)).status_code == 200
    (entry1,) = await _wait_record_count(bridge, text1, 1)
    assert entry1["value"] == first

    # Re-navigate the same session (present the cookie) with a DIFFERENT param → replaced.
    second = uniq("l22c-second")
    reload_ok = await get_chat_page(_base_url(bridge), identity, query={"x": second}, cookies=web.cookies)
    assert reload_ok.status_code == 200
    text2 = uniq("l22c-msg-2")
    assert (await web.send(text2)).status_code == 200
    (entry2,) = await _wait_record_count(bridge, text2, 1)
    assert entry2["value"] == second

    # Re-navigate with NO params → the stored params are left as they are.
    bare = await get_chat_page(_base_url(bridge), identity, cookies=web.cookies)
    assert bare.status_code == 200
    text3 = uniq("l22c-msg-3")
    assert (await web.send(text3)).status_code == 200
    (entry3,) = await _wait_record_count(bridge, text3, 1)
    assert entry3["value"] == second, "a param-less re-navigation must not clear the stored params"


async def test_rotate_mints_a_clean_registration_with_no_params(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """A rotation is the visitor's "new conversation": its fresh registration carries NO link
    params, so the tool payload for a rotated session has no ``params`` key at all."""
    identity = await _web_tool_route(
        bridge, uniq, "l22d", payload_expr=_record_expr(".message", '(has("params") | tostring)')
    )
    store_url = bridge.stack.resources.redis_url

    web, _page = await WebChatClient.open_page(_base_url(bridge), identity, store_url=store_url, query={"x": uniq("v")})
    rotated, response = await web.rotate(store_url=store_url)
    assert response.status_code == 200, response.text
    assert rotated is not None
    assert rotated.visitor_id != web.visitor_id

    text = uniq("l22d-msg")
    assert (await rotated.send(text)).status_code == 200
    (entry,) = await _wait_record_count(bridge, text, 1)
    assert entry["value"] == "false", "a rotated session must deliver a payload with no params key"


async def test_a_no_params_visitor_delivers_a_byte_identical_payload(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """A visitor whose entry carried no params gets the web channel's base tool payload with
    NO ``params`` key added. The regression fence: link params are additive, so a no-params
    turn's payload is byte-identical to before the feature — its keys are exactly the base set
    (``channel``, ``message``, ``our_identity``, ``sender``), ``params`` absent."""
    payload_expr = _record_expr(".message", '(keys | join(","))')
    identity = await _web_tool_route(bridge, uniq, "l22e", payload_expr=payload_expr)

    web, _page = await WebChatClient.open_page(_base_url(bridge), identity, store_url=bridge.stack.resources.redis_url)
    text = uniq("l22e-msg")
    assert (await web.send(text)).status_code == 200
    (entry,) = await _wait_record_count(bridge, text, 1)
    # jq ``keys`` is sorted; the base set carries no ``params`` for a no-params entry.
    assert entry["value"] == "channel,message,our_identity,sender,thread_id", "a no-params turn must add no params key"


async def test_bounds_violations_answer_a_400_page_and_mint_no_session(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """Each params bound — too many, oversize value, bad key charset, duplicate key — is a
    byte-constant 400 refusal PAGE carrying ``link_params_invalid`` in its meta, and NO
    session cookie is minted (the door refused before establishing one)."""
    identity = await _web_tool_route(bridge, uniq, "l22f", payload_expr=_record_expr(".message", ".params.x"))
    base = _base_url(bridge)

    too_many = {f"k{i}": "v" for i in range(17)}
    oversize = {"x": "z" * 513}
    bad_key = {"bad.key": "v"}
    # A raw query string is the type-clean way to present a DUPLICATE key to httpx.
    duplicate = "x=1&x=2"

    for query in (too_many, oversize, bad_key, duplicate):
        response = await get_chat_page(base, identity, query=query)
        assert response.status_code == 400, response.text
        assert '<meta name="tai42-refusal-code" content="link_params_invalid">' in response.text
        assert SESSION_COOKIE not in response.cookies, "a refused navigation must mint no session"


async def test_reserved_query_names_never_reach_the_delivered_params(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """``tai_pair`` and ``tai_entry`` are the door's own coordinates: they are stripped before
    params are built, so a navigation carrying them delivers only the real params and never
    ``tai_pair`` / ``tai_entry`` under ``.params``."""
    probe = uniq("l22-reserved")
    identity = await _web_tool_route(
        bridge, uniq, "l22g", payload_expr=_record_expr(json.dumps(probe), '(.params | keys | join(","))')
    )

    web, _page = await WebChatClient.open_page(
        _base_url(bridge),
        identity,
        store_url=bridge.stack.resources.redis_url,
        query={"tai_pair": "PAIRVAL", "tai_entry": "ENTRYVAL", "a": "1"},
    )
    assert (await web.send(uniq("l22g-msg"))).status_code == 200
    (entry,) = await _wait_record_count(bridge, probe, 1)
    delivered_keys = entry["value"].split(",") if entry["value"] else []
    assert delivered_keys == ["a"], f"only real params are delivered, got {delivered_keys!r}"
    assert "tai_pair" not in delivered_keys
    assert "tai_entry" not in delivered_keys


async def test_api_door_carries_params_and_refuses_an_invalid_value_without_echoing_it(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """The authed API door takes the same ``params`` field on its message body: valid params
    reach the tool payload; an invalid one is the door's 400, whose body names the violated
    bound but NEVER the offending value (pydantic's default ``input_value`` is not reflected)."""
    probe = uniq("l22-api")
    route = uniq("l22h-route").replace("_", "-")
    exec_key = uniq("l22h-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_api_route(
        route_name=route,
        tool="e2e_record",
        execution_key=exec_key,
        callback_url=_UNREACHABLE_CALLBACK,
        payload_expr=_record_expr(json.dumps(probe), '(.params.x // "none")'),
        reply_expr="null",
    )
    caller = await bridge.mint_key(user_id=uniq("l22h-caller"), scopes=["e2e-all"])

    delivered = uniq("l22h-val")
    await bridge.api(token=caller).post(
        f"/api/conversations/{route}/messages",
        json={"external_user_id": uniq("l22h-user"), "text": "ping", "params": {"x": delivered}, "wait_seconds": 20},
        expect=200,
    )
    recorded = await wait_probe_record(bridge, probe)
    assert [entry["value"] for entry in recorded] == [delivered]

    # An oversize value: the door refuses 400 naming the bound, never echoing the value.
    secret = uniq("l22h-secret")
    invalid_value = secret + "z" * 600
    refused = await bridge.api(token=caller).request_raw(
        "POST",
        f"/api/conversations/{route}/messages",
        json={"external_user_id": uniq("l22h-user2"), "text": "ping", "params": {"x": invalid_value}},
    )
    assert refused.status_code == 400, refused.text
    assert "over the" in refused.text, refused.text
    assert "512" in refused.text, refused.text
    assert secret not in refused.text, "the refusal must not echo the offending param value"
