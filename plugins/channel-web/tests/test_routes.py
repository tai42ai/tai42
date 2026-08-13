"""The public doors — the chat page + asset serving, session minting/registration and
its navigation guard, the binding of a session to the web route it was minted on,
message bridging and its retry key, the SSE stream gate, the answer forward status
policy, and session rotation. Handlers are driven directly with hand-rolled requests;
the visitor's cookie token rides the request and the doors resolve it to the
registered visitor id."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.responses import FileResponse, Response, StreamingResponse
from tai42_contract.conversations import BlankInboundTextError
from tai42_kit.settings import reset_all_settings

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web import stream as stream_module
from tai42_channel_web.channel import WebChannel
from tai42_channel_web.page import PAGE_CSP, REFUSAL_CODE_META, REFUSAL_CSP
from tai42_channel_web.routes import AnswerForwardError
from tai42_channel_web.store import (
    QuestionRecord,
    mint_entry_code,
    reserve_question,
    resolve_session,
    revoke_entry_code,
    set_gate,
    transcript_order,
)
from tests.conftest import (
    CALLBACK,
    CLIENT_HOST,
    ENTRY_ASSET,
    IDENTITY,
    ORIGIN,
    OTHER_IDENTITY,
    PLAIN_COOKIE,
    SECURE_COOKIE,
    SESSION_TOKEN,
    STYLE_ASSET,
    VISITOR_ID,
    FakeHttpx,
    FakeRedis,
    build_request,
    make_notification,
    register,
    response,
    write_manifest,
)

_CHAT = "/api/channels/web/chat/{identity}"
_ASSETS = "/api/channels/web/assets/{file}"
_MESSAGES = "/api/channels/web/messages"
_STREAM = "/api/channels/web/stream"
_ANSWER = "/api/channels/web/questions/{interaction_id}/answer"
_ROTATE = "/api/channels/web/session/rotate"
_TRANSCRIPT_KEY = f"channel:web:transcript:{IDENTITY}:{VISITOR_ID}"
_SESSION_KEY = f"channel:web:session:{SESSION_TOKEN}"
_FOREIGN_ORIGIN = [(b"origin", b"https://evil.example")]
# What a browser sends on a cross-site subresource load, as opposed to a navigation.
_SUBRESOURCE = [(b"sec-fetch-dest", b"image")]
_NAVIGATION = [(b"sec-fetch-dest", b"document")]


def _rotate_request(identity: str = IDENTITY, **kwargs):
    """A rotation POST. The body names the web route the fresh session is minted for
    — a session belongs to one."""
    return build_request(path=_ROTATE, json_body={"identity": identity}, **kwargs)


def _handler(stub_app, path: str) -> Callable[..., Awaitable[Response]]:
    routes = [route for route in stub_app.http.routes if route.path == path]
    assert len(routes) == 1
    # Every door is public: the visitor session cookie is the only credential.
    assert routes[0].authed is False
    return routes[0].handler


async def _close_body(resp: StreamingResponse) -> None:
    """Close a streaming body the way an abandoned client does — the generator's
    ``finally`` is what gives its stream slot back."""
    await cast(AsyncGenerator[str], resp.body_iterator).aclose()


def _body(resp: Response) -> dict:
    return json.loads(bytes(resp.body))


async def _sent_body(resp: Response) -> bytes:
    """The bytes a response actually puts on the wire — the only way to read a
    ``FileResponse``, which streams from disk rather than carrying a ``.body``."""
    chunks: list[bytes] = []

    async def send(message: Any) -> None:
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    await resp({"type": "http", "method": "GET", "headers": []}, receive, send)
    return b"".join(chunks)


def _refusal(resp: Response, code: str | None = None) -> str:
    """The HTML of a page-door refusal. The door is reached by NAVIGATING to it, so
    every refusal must render as a page rather than as a body the browser shows as
    text — self-contained under the refusal CSP (nothing to fetch, nothing to run, and
    no inline style, which that CSP forbids), and still carrying the machine-readable
    code for whoever reads the refused navigation."""
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    assert resp.headers["content-security-policy"] == REFUSAL_CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-store"
    html = bytes(resp.body).decode()
    assert html.startswith("<!doctype html>")
    for forbidden in ("<script", "<link", "<style", "style="):
        assert forbidden not in html
    if code is None:
        assert REFUSAL_CODE_META not in html
    else:
        assert f'<meta name="{REFUSAL_CODE_META}" content="{code}">' in html
    return html


def _set_cookie(resp: Response) -> SimpleCookie:
    raw = resp.headers.getlist("set-cookie")
    assert len(raw) == 1
    cookie = SimpleCookie()
    cookie.load(raw[0])
    return cookie


# -- GET /chat/{identity} -------------------------------------------------------


def _chat_request(identity: str = IDENTITY, **kwargs):
    return build_request(
        method="GET", path=_CHAT.format(identity=identity), path_params={"identity": identity}, **kwargs
    )


async def test_chat_page_renders_the_built_shell(web_env, stub_app, fake_redis: FakeRedis, public_build: Path):
    resp = await _handler(stub_app, _CHAT)(_chat_request())

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    assert resp.headers["content-security-policy"] == PAGE_CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"] == "no-store"
    html = bytes(resp.body).decode()
    assert f'src="/api/channels/web/assets/{ENTRY_ASSET}"' in html
    assert f'href="/api/channels/web/assets/{STYLE_ASSET}"' in html
    assert f'data-identity="{IDENTITY}"' in html
    assert "<title>Chat</title>" in html


async def test_chat_page_mints_and_registers_a_session_when_absent(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    resp = await _handler(stub_app, _CHAT)(_chat_request())

    morsel = _set_cookie(resp)[SECURE_COOKIE]
    assert len(morsel.value) >= 22
    assert morsel["httponly"] is True
    assert morsel["secure"] is True
    assert morsel["samesite"] == "lax"
    # The ``__Host-`` prefix the Secure default mints under is honored only at the
    # root path.
    assert morsel["path"] == "/"
    assert morsel["max-age"] == str(30 * 86400)
    # The cookie is a session only because a registration stands behind it; the
    # address it resolves to is NOT the cookie value, and it is bound to the web route
    # the page was opened on.
    registration = await resolve_session(morsel.value)
    assert registration is not None
    assert registration.visitor_id != morsel.value
    assert registration.identity == IDENTITY


async def test_chat_page_keeps_a_registered_session_and_refreshes_it(
    web_env, stub_app, registered_session: FakeRedis, public_build: Path
):
    registered_session.ttls[_SESSION_KEY] = 5
    resp = await _handler(stub_app, _CHAT)(_chat_request(token=SESSION_TOKEN))
    assert _set_cookie(resp)[SECURE_COOKIE].value == SESSION_TOKEN
    assert registered_session.ttls[_SESSION_KEY] == 30 * 86400


async def test_chat_page_never_adopts_an_unregistered_token(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    # Session fixation: a planted, shape-valid cookie is replaced, never adopted —
    # so the planter's value never becomes the visitor's conversation.
    resp = await _handler(stub_app, _CHAT)(_chat_request(token=SESSION_TOKEN))
    minted = _set_cookie(resp)[SECURE_COOKIE].value
    assert minted != SESSION_TOKEN
    assert await resolve_session(minted) is not None


async def test_chat_page_replaces_a_malformed_session_cookie(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    # A value outside the minted alphabet/length is no session at all — it must never
    # reach the transcript key as an address.
    resp = await _handler(stub_app, _CHAT)(_chat_request(cookie=f"{SECURE_COOKIE}=nope:short"))
    assert _set_cookie(resp)[SECURE_COOKIE].value != "nope:short"


async def test_chat_page_escapes_the_identity(web_env, stub_app, fake_redis: FakeRedis, public_build: Path):
    resp = await _handler(stub_app, _CHAT)(_chat_request(identity='a"><script>x</script>'))
    html = bytes(resp.body).decode()
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


async def test_chat_page_title_comes_from_settings(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path, monkeypatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_PAGE_TITLE", "Ask <us> anything")
    reset_all_settings()
    resp = await _handler(stub_app, _CHAT)(_chat_request())
    assert "<title>Ask &lt;us&gt; anything</title>" in bytes(resp.body).decode()


async def test_chat_page_cookie_naming_follows_the_secure_setting(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path, monkeypatch
):
    # A plain-http deployment mints the bare name at this plugin's own path: a
    # browser refuses a ``__Host-`` cookie that is not Secure at ``/``, so the
    # prefixed one there would leave every visitor session-less.
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_SESSION_COOKIE_SECURE", "false")
    reset_all_settings()
    resp = await _handler(stub_app, _CHAT)(_chat_request())
    morsel = _set_cookie(resp)[PLAIN_COOKIE]
    assert morsel["secure"] == ""
    assert morsel["path"] == "/api/channels/web"
    # …and it reads that name back, minting nothing for a returning visitor.
    registration = await resolve_session(morsel.value)
    assert registration is not None
    assert registration.identity == IDENTITY
    kept = await _handler(stub_app, _CHAT)(_chat_request(token=morsel.value, cookie_name=PLAIN_COOKIE))
    assert _set_cookie(kept)[PLAIN_COOKIE].value == morsel.value


async def test_chat_page_unconfigured_store_is_an_html_501(no_web_env, stub_app, public_build: Path):
    # Without a store there is nowhere to register a session, so the page cannot be
    # served as a working chat — it refuses with a page carrying the code, never with
    # a raw body the visitor's browser would render as text.
    resp = await _handler(stub_app, _CHAT)(_chat_request())
    assert resp.status_code == 501
    assert "<h1>Chat is unavailable</h1>" in _refusal(resp, "web_transcript_store_off")


async def test_chat_page_unbuilt_bundle_is_a_loud_500_without_server_paths(
    web_env, stub_app, fake_redis: FakeRedis, tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
):
    from tai42_channel_web import page

    monkeypatch.setattr(page, "_public_dir", lambda: tmp_path / "absent")
    with caplog.at_level("ERROR"):
        resp = await _handler(stub_app, _CHAT)(_chat_request())
    assert resp.status_code == 500
    # An anonymous visitor gets a page, and is told nothing about the server's
    # filesystem or the build that failed…
    html = _refusal(resp)
    assert "<h1>Chat is unavailable</h1>" in html
    assert "pnpm" not in html
    assert str(tmp_path) not in html
    # …while the operator gets the path and the build step in the log.
    assert any("pnpm" in record.getMessage() for record in caplog.records)


async def test_chat_page_refuses_to_mint_for_a_subresource_load(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    # Minting is a state change on a GET: a cross-site <img>/<script> pointed at the
    # chat URL would otherwise overwrite a live visitor's cookie and strand them.
    resp = await _handler(stub_app, _CHAT)(_chat_request(extra_headers=_SUBRESOURCE))

    assert resp.status_code == 403
    _refusal(resp, "not_a_navigation")
    assert resp.headers.getlist("set-cookie") == []
    assert fake_redis.store == {}


async def test_chat_page_mints_for_a_declared_navigation(web_env, stub_app, fake_redis: FakeRedis, public_build: Path):
    resp = await _handler(stub_app, _CHAT)(_chat_request(extra_headers=_NAVIGATION))
    assert resp.status_code == 200
    assert await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value) is not None


async def test_chat_page_still_serves_a_registered_session_to_a_subresource_load(
    web_env, stub_app, registered_session: FakeRedis, public_build: Path
):
    # The guard is on MINTING only — a visitor who already has a session mints
    # nothing, so there is no cookie to overwrite.
    resp = await _handler(stub_app, _CHAT)(_chat_request(token=SESSION_TOKEN, extra_headers=_SUBRESOURCE))
    assert resp.status_code == 200
    assert _set_cookie(resp)[SECURE_COOKIE].value == SESSION_TOKEN


# -- a session is bound to the web route it was minted on -----------------------


async def test_the_chat_page_replaces_a_session_minted_on_another_route(
    web_env, stub_app, registered_session: FakeRedis, public_build: Path
):
    # A cookie is one per browser and per origin, so opening a second route's chat
    # page must mint that route's own session rather than carry the first one over —
    # and it is replaced exactly as an unregistered cookie is.
    resp = await _handler(stub_app, _CHAT)(_chat_request(identity=OTHER_IDENTITY, token=SESSION_TOKEN))

    minted = _set_cookie(resp)[SECURE_COOKIE].value
    assert minted != SESSION_TOKEN
    registration = await resolve_session(minted)
    assert registration is not None
    assert registration.identity == OTHER_IDENTITY
    assert registration.visitor_id != VISITOR_ID


async def test_the_chat_page_of_a_foreign_session_still_needs_a_navigation_to_mint(
    web_env, stub_app, registered_session: FakeRedis, public_build: Path
):
    # A foreign session is no session here, so the mint guard applies to it in full:
    # a cross-site subresource must not swap a live visitor's cookie for another
    # route's fresh one.
    resp = await _handler(stub_app, _CHAT)(
        _chat_request(identity=OTHER_IDENTITY, token=SESSION_TOKEN, extra_headers=_SUBRESOURCE)
    )

    assert resp.status_code == 403
    _refusal(resp, "not_a_navigation")
    assert resp.headers.getlist("set-cookie") == []


async def test_messages_on_the_session_own_route_are_bridged(web_env, stub_app, registered_session: FakeRedis):
    body = {"identity": IDENTITY, "text": "hello"}
    resp = await _handler(stub_app, _MESSAGES)(build_request(json_body=body, token=SESSION_TOKEN))

    assert resp.status_code == 200
    assert stub_app.conversations.accept_calls[0]["our_identity"] == IDENTITY


async def test_messages_on_another_route_are_refused_exactly_like_no_session(
    web_env, stub_app, registered_session: FakeRedis, fake_redis: FakeRedis
):
    # THE bind: a session minted on one web route buys nothing on another. The
    # refusal must be the one a caller with no cookie at all gets — same status, same
    # code, same message — or the door would confirm that the cookie is a live
    # session for some other route.
    handler = _handler(stub_app, _MESSAGES)
    foreign = await handler(build_request(json_body={"identity": OTHER_IDENTITY, "text": "x"}, token=SESSION_TOKEN))
    absent = await handler(build_request(json_body={"identity": OTHER_IDENTITY, "text": "x"}))

    assert foreign.status_code == absent.status_code == 401
    assert _body(foreign) == _body(absent)
    assert foreign.headers.items() == absent.headers.items()
    # And it buys no turn: the bridge is never reached.
    assert stub_app.conversations.accept_calls == []


async def test_the_stream_of_another_route_is_refused_exactly_like_no_session(
    web_env, stub_app, registered_session: FakeRedis
):
    handler = _handler(stub_app, _STREAM)
    foreign = await handler(_stream_request(query=f"identity={OTHER_IDENTITY}", token=SESSION_TOKEN))
    absent = await handler(_stream_request(query=f"identity={OTHER_IDENTITY}"))

    assert foreign.status_code == absent.status_code == 401
    assert _body(foreign) == _body(absent)
    assert foreign.headers.items() == absent.headers.items()


async def test_the_stream_of_the_session_own_route_is_served(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))
    assert isinstance(resp, StreamingResponse)
    await _close_body(resp)


async def test_a_question_asked_on_another_route_is_not_this_session_s_to_answer(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # The answer door reads no identity from the request, so the bind is checked
    # against the record: same address, other route — still not this caller's
    # conversation, and reported as not found like any foreign question.
    await _seed_question(identity=OTHER_IDENTITY)

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))

    assert resp.status_code == 404
    assert "question not found" in _body(resp)["error"]
    assert fake_httpx.calls == []
    assert "channel:web:question:int-1" in registered_session.store


async def test_a_rotation_binds_the_fresh_session_to_the_route_it_names(
    web_env, stub_app, registered_session: FakeRedis
):
    resp = await _handler(stub_app, _ROTATE)(_rotate_request(OTHER_IDENTITY, token=SESSION_TOKEN))

    assert resp.status_code == 200
    registration = await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value)
    assert registration is not None
    assert registration.identity == OTHER_IDENTITY


@pytest.mark.parametrize("body", [{}, {"identity": "  "}, {"identity": "site:alpha"}])
async def test_a_rotation_without_a_usable_route_is_422(web_env, stub_app, registered_session: FakeRedis, body: dict):
    # The fresh session must belong to a route; a rotation that names none would mint
    # a session no door could ever serve.
    resp = await _handler(stub_app, _ROTATE)(build_request(path=_ROTATE, json_body=body, token=SESSION_TOKEN))

    assert resp.status_code == 422
    assert "identity" in _body(resp)["error"]
    # The session it was asked to replace is untouched.
    registration = await resolve_session(SESSION_TOKEN)
    assert registration is not None
    assert registration.visitor_id == VISITOR_ID


async def test_two_routes_in_one_browser_never_share_a_conversation(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    # End to end, the way a visitor meets it: open route A, open route B, and the
    # cookie B leaves behind addresses B's conversation only.
    chat = _handler(stub_app, _CHAT)
    token_a = _set_cookie(await chat(_chat_request()))[SECURE_COOKIE].value
    token_b = _set_cookie(await chat(_chat_request(identity=OTHER_IDENTITY, token=token_a)))[SECURE_COOKIE].value

    messages = _handler(stub_app, _MESSAGES)
    to_b = await messages(build_request(json_body={"identity": OTHER_IDENTITY, "text": "x"}, token=token_b))
    to_a = await messages(build_request(json_body={"identity": IDENTITY, "text": "x"}, token=token_b))

    assert to_b.status_code == 200
    assert to_a.status_code == 401
    assert [call["our_identity"] for call in stub_app.conversations.accept_calls] == [OTHER_IDENTITY]


# -- GET /assets/{file} ---------------------------------------------------------


def _asset_request(name: str):
    return build_request(method="GET", path=_ASSETS.format(file=name), path_params={"file": name})


async def test_asset_serves_a_listed_file(web_env, stub_app, public_build: Path):
    resp = await _handler(stub_app, _ASSETS)(_asset_request(ENTRY_ASSET))

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/javascript; charset=utf-8"
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert await _sent_body(resp) == (public_build / ENTRY_ASSET).read_bytes()


async def test_asset_is_streamed_not_read_on_the_event_loop(web_env, stub_app, public_build: Path):
    # A bundle file is hundreds of kilobytes; reading it into the handler would block
    # the loop for the whole file on every single request, uncached.
    resp = await _handler(stub_app, _ASSETS)(_asset_request(ENTRY_ASSET))
    assert isinstance(resp, FileResponse)


async def test_asset_is_stat_ed_once_per_request(
    web_env, stub_app, public_build: Path, monkeypatch: pytest.MonkeyPatch
):
    # The door has to stat to tell a broken build from a served file, and the response
    # needs the same numbers for its length/etag headers. Handing it the result is one
    # thread-pool round trip per request instead of two, on every asset of every page
    # load.
    real_stat = os.stat
    stats: list[str] = []

    def _counting_stat(path, *args: Any, **kwargs: Any):
        if str(path).endswith(ENTRY_ASSET):
            stats.append(str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _counting_stat)
    resp = await _handler(stub_app, _ASSETS)(_asset_request(ENTRY_ASSET))
    await _sent_body(resp)

    assert len(stats) == 1
    assert resp.headers["content-length"] == str((public_build / ENTRY_ASSET).stat().st_size)


async def test_asset_stylesheet_content_type(web_env, stub_app, public_build: Path):
    resp = await _handler(stub_app, _ASSETS)(_asset_request(STYLE_ASSET))
    assert resp.headers["content-type"] == "text/css; charset=utf-8"


@pytest.mark.parametrize("name", ["public-manifest.json", "not-built.js", "../settings.py"])
async def test_asset_unlisted_file_is_404(web_env, stub_app, public_build: Path, name: str):
    # The integrity map is the allowlist: anything not emitted by the build — the
    # manifest itself included — is simply not reachable.
    resp = await _handler(stub_app, _ASSETS)(_asset_request(name))
    assert resp.status_code == 404


async def test_asset_listed_but_missing_on_disk_is_a_loud_500(
    web_env, stub_app, public_build: Path, caplog: pytest.LogCaptureFixture
):
    (public_build / ENTRY_ASSET).unlink()
    with caplog.at_level("ERROR"):
        resp = await _handler(stub_app, _ASSETS)(_asset_request(ENTRY_ASSET))
    assert resp.status_code == 500
    assert _body(resp) == {"error": "the chat page is unavailable"}
    assert any("listed in the build manifest but is not a readable file" in r.getMessage() for r in caplog.records)


async def test_asset_unbuilt_bundle_is_a_loud_500(web_env, stub_app, tmp_path: Path, monkeypatch):
    from tai42_channel_web import page

    monkeypatch.setattr(page, "_public_dir", lambda: tmp_path / "absent")
    resp = await _handler(stub_app, _ASSETS)(_asset_request(ENTRY_ASSET))
    assert resp.status_code == 500


async def test_asset_unmapped_extension_is_octet_stream(web_env, stub_app, public_build: Path):
    from tai42_channel_web import page

    (public_build / "data.bin").write_bytes(b"\x00\x01")
    write_manifest(public_build, integrity={ENTRY_ASSET: "sha384-e", STYLE_ASSET: "sha384-s", "data.bin": "sha384-b"})
    page.load_build.cache_clear()
    resp = await _handler(stub_app, _ASSETS)(_asset_request("data.bin"))
    assert resp.headers["content-type"] == "application/octet-stream"


async def test_the_manifest_is_parsed_once_per_process(web_env, stub_app, public_build: Path):
    # Every page and asset request would otherwise be a synchronous disk read on the
    # event loop; the bundle ships in the wheel and cannot change under the server.
    await _handler(stub_app, _ASSETS)(_asset_request(ENTRY_ASSET))
    (public_build / "public-manifest.json").unlink()
    resp = await _handler(stub_app, _ASSETS)(_asset_request(ENTRY_ASSET))
    assert resp.status_code == 200


# -- POST /messages -------------------------------------------------------------


async def test_messages_bridges_and_appends_inbound(web_env, stub_app, registered_session: FakeRedis):
    stub_app.conversations.accept_result = "turn-42"
    handler = _handler(stub_app, _MESSAGES)

    resp = await handler(build_request(json_body={"identity": IDENTITY, "text": "ship it"}, token=SESSION_TOKEN))

    assert resp.status_code == 200
    assert _body(resp) == {"data": {"message_id": "turn-42"}}
    assert resp.headers["x-content-type-options"] == "nosniff"
    call = stub_app.conversations.accept_calls[0]
    assert call["channel"] == "web"
    assert call["our_identity"] == IDENTITY
    # The address is the REGISTERED visitor id — never the cookie's secret, and never
    # a body value.
    assert call["client_address"] == VISITOR_ID
    assert call["text"] == "ship it"
    assert len(call["provider_message_id"]) == 32
    # The inbound entry is appended only after accept, reusing the turn id.
    payload = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert payload == {"id": "turn-42", "direction": "in", "text": "ship it", "ts": payload["ts"]}


async def test_the_turn_cap_keys_on_the_network_bucket_not_the_resettable_visitor_id(
    web_env, stub_app, fake_redis: FakeRedis
):
    # The rotate-reset attack: the platform mints the visitor id on an unauthenticated
    # door and the visitor rotates it at will, so a cap keyed on the visitor id would
    # reset every message. Two sessions minted from the SAME network client must share
    # ONE accountable cap key — the request's network bucket — so the cap bounds them.
    _SECOND_TOKEN = "MZ3n-visitor_session-token-9876543210"
    _SECOND_VISITOR = "vis-9876543210cd"
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY)
    register(fake_redis, _SECOND_TOKEN, _SECOND_VISITOR, IDENTITY)
    handler = _handler(stub_app, _MESSAGES)

    await handler(build_request(json_body={"identity": IDENTITY, "text": "one"}, token=SESSION_TOKEN))
    await handler(build_request(json_body={"identity": IDENTITY, "text": "two"}, token=_SECOND_TOKEN))

    first, second = stub_app.conversations.accept_calls
    # Two distinct conversations (two minted visitor ids)...
    assert first["client_address"] == VISITOR_ID
    assert second["client_address"] == _SECOND_VISITOR
    assert first["client_address"] != second["client_address"]
    # ...but ONE accountable cap key: the network bucket, never the resettable id.
    assert first["cap_key"] == second["cap_key"] == CLIENT_HOST
    assert first["cap_key"] != first["client_address"]


async def test_the_reply_message_id_is_the_id_on_the_visitor_own_sse_frame(
    web_env, stub_app, registered_session: FakeRedis
):
    # The page draws a message optimistically on POST and retires that bubble when the
    # same id arrives on the stream. Anything short of EXACT equality between the
    # reply's ``message_id`` and the ``chat.message`` frame's ``id`` leaves the
    # optimistic bubble standing next to the replayed one — two bubbles for one
    # message.
    stub_app.conversations.accept_result = "turn-42"
    sent = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "ship it"}, token=SESSION_TOKEN)
    )
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))

    assert isinstance(resp, StreamingResponse)
    frames = [str(frame) async for frame in resp.body_iterator]
    event, _, payload = frames[0].partition("\ndata: ")
    assert event == "event: chat.message"
    assert json.loads(payload)["id"] == _body(sent)["data"]["message_id"] == "turn-42"


async def test_messages_canonicalises_the_identity(web_env, stub_app, registered_session: FakeRedis):
    # The bridge trims; an untrimmed identity here would key a transcript the
    # bridge's own writes never reach.
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": f"  {IDENTITY} ", "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 200
    assert stub_app.conversations.accept_calls[0]["our_identity"] == IDENTITY
    assert _TRANSCRIPT_KEY in registered_session.streams


async def test_messages_without_a_session_cookie_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(build_request(json_body={"identity": IDENTITY, "text": "x"}))
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"


async def test_messages_with_an_unregistered_token_is_401(web_env, stub_app, fake_redis: FakeRedis):
    # The invented-cookie bypass: a shape-valid token nobody registered opens no
    # conversation, so it cannot mint a fresh address past the bridge's turn caps.
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"
    assert stub_app.conversations.accept_calls == []


async def test_messages_with_a_malformed_session_cookie_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, cookie=f"{SECURE_COOKIE}=a:b")
    )
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"


async def test_messages_cross_origin_is_403(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN, extra_headers=_FOREIGN_ORIGIN)
    )
    assert resp.status_code == 403
    assert _body(resp)["code"] == "origin_mismatch"


async def test_messages_same_origin_header_is_accepted(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(
            json_body={"identity": IDENTITY, "text": "x"},
            token=SESSION_TOKEN,
            extra_headers=[(b"origin", ORIGIN.encode())],
        )
    )
    assert resp.status_code == 200


async def test_messages_unconfigured_store_is_501(no_web_env, stub_app):
    # A turn must not start when its reply can never be shown.
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 501
    assert _body(resp)["code"] == "web_transcript_store_off"
    assert stub_app.conversations.accept_calls == []


async def test_messages_invalid_json(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(build_request(raw_body=b"{not json", token=SESSION_TOKEN))
    assert resp.status_code == 400


async def test_messages_oversized_body_is_413(web_env, stub_app, registered_session: FakeRedis, monkeypatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_MAX_BODY_BYTES", "64")
    reset_all_settings()
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x" * 500}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 413
    assert stub_app.conversations.accept_calls == []


async def test_messages_over_long_text_is_422(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x" * 8001}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 422
    assert stub_app.conversations.accept_calls == []


@pytest.mark.parametrize("identity", ["  ", "site:alpha", "a" * 257])
async def test_messages_rejects_an_unusable_identity(web_env, stub_app, registered_session: FakeRedis, identity: str):
    # A ``:`` would split the composite recipient and the transcript key.
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": identity, "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 422


async def test_messages_blank_text_maps_to_400(web_env, stub_app, registered_session: FakeRedis):
    stub_app.conversations.accept_error = BlankInboundTextError("blank")
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "   "}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 400
    # A refused message never pollutes the transcript.
    assert not registered_session.streams


async def test_messages_unrouted_maps_to_404(web_env, stub_app, registered_session: FakeRedis):
    stub_app.conversations.accept_error = LookupError("no web route named 'site-alpha'")
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 404
    assert "no web route" in _body(resp)["error"]


async def test_messages_thread_overflow_maps_to_503(web_env, stub_app, registered_session: FakeRedis):
    class ThreadQueueOverflowError(Exception):
        pass

    stub_app.conversations.accept_error = ThreadQueueOverflowError("thread queue full")
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 503


async def test_messages_mints_a_fresh_dedup_id_without_a_retry_key(web_env, stub_app, registered_session: FakeRedis):
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "x"}
    await handler(build_request(json_body=body, token=SESSION_TOKEN))
    await handler(build_request(json_body=body, token=SESSION_TOKEN))

    minted = [call["provider_message_id"] for call in stub_app.conversations.accept_calls]
    assert len(set(minted)) == 2
    assert all(len(value) == 32 for value in minted)


async def test_messages_derive_the_dedup_id_from_a_retry_key_and_the_visitor(
    web_env, stub_app, registered_session: FakeRedis
):
    # A POST whose reply never reached the browser is re-sent with the SAME key; the
    # bridge dedups on (channel, provider_message_id) and returns the first turn, so
    # Retry cannot deliver the message twice.
    stub_app.conversations.accept_result = "turn-first"
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "ship it", "client_message_id": "abc-123_XY"}

    first = await handler(build_request(json_body=body, token=SESSION_TOKEN))
    retry = await handler(build_request(json_body=body, token=SESSION_TOKEN))

    assert _body(first) == _body(retry) == {"data": {"message_id": "turn-first"}}
    sent = [call["provider_message_id"] for call in stub_app.conversations.accept_calls]
    assert sent[0] == sent[1]
    # Scoped to the whole conversation — the web route AND the caller's own address,
    # neither of which is the caller's to choose. One visitor can therefore never
    # reach into another's dedup space, and one conversation never into another's.
    assert sent[0] == hashlib.sha256(f"{IDENTITY}:{VISITOR_ID}:abc-123_XY".encode()).hexdigest()


async def test_the_same_retry_key_from_another_visitor_is_a_different_dedup_id(
    web_env, stub_app, fake_redis: FakeRedis
):
    other_token = "OTHER-visitor_session-token-0123456789"
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY)
    register(fake_redis, other_token, "vis-other-000", IDENTITY)
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "x", "client_message_id": "shared-key-1"}

    await handler(build_request(json_body=body, token=SESSION_TOKEN))
    await handler(build_request(json_body=body, token=other_token))

    sent = [call["provider_message_id"] for call in stub_app.conversations.accept_calls]
    assert sent[0] != sent[1]


async def test_the_same_retry_key_on_another_web_route_is_a_different_dedup_id(
    web_env, stub_app, registered_session: FakeRedis
):
    # A conversation is ``(identity, address)`` everywhere, and the bridge dedups
    # identity-blind. Keyed on the retry key alone, the second route's send would
    # resolve to the FIRST route's turn: no turn would run, and the new text would be
    # appended to the first route's transcript under the old turn's id. The visitor
    # reaches the second route with its OWN session — a session serves one route.
    other_token = "BETA-visitor_session-token-01234567890"
    other_visitor = "vis-beta-00000"
    register(registered_session, other_token, other_visitor, OTHER_IDENTITY)
    stub_app.conversations.accept_result = "turn-alpha"
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "for alpha", "client_message_id": "shared-key-1"}
    other = {"identity": OTHER_IDENTITY, "text": "for beta", "client_message_id": "shared-key-1"}

    first = await handler(build_request(json_body=body, token=SESSION_TOKEN))
    stub_app.conversations.accept_result = "turn-beta"
    second = await handler(build_request(json_body=other, token=other_token))

    sent = [call["provider_message_id"] for call in stub_app.conversations.accept_calls]
    assert sent[0] != sent[1]
    # Both turns ran, and each text landed on its own route's transcript.
    assert _body(first) == {"data": {"message_id": "turn-alpha"}}
    assert _body(second) == {"data": {"message_id": "turn-beta"}}
    assert [json.loads(e[1]["data"])["text"] for e in registered_session.streams[_TRANSCRIPT_KEY]] == ["for alpha"]
    beta_key = f"channel:web:transcript:{OTHER_IDENTITY}:{other_visitor}"
    assert [json.loads(e[1]["data"])["text"] for e in registered_session.streams[beta_key]] == ["for beta"]


class _IdempotentConversations:
    """The bridge's own dedup contract as ``accept`` states it: idempotent on
    ``(channel, provider_message_id)`` — a redelivery returns the first attempt's
    ``message_id`` and starts no second turn."""

    def __init__(self) -> None:
        self.accept_calls: list[dict[str, Any]] = []
        self.turns: dict[tuple[str, str], str] = {}

    async def accept(self, **kwargs: Any) -> str:
        self.accept_calls.append(kwargs)
        key = (kwargs["channel"], kwargs["provider_message_id"])
        owner = self.turns.get(key)
        if owner is not None:
            return owner
        return self.turns.setdefault(key, f"turn-{len(self.turns) + 1}")


async def test_a_retry_of_the_same_key_replaces_its_own_frame_in_place(
    web_env, stub_app, registered_session: FakeRedis
):
    # The other trigger of a dedup collision: the SAME conversation re-POSTing the
    # key. That is what a retry is — the bridge returns the first attempt's turn, and
    # the frame is re-appended under that same id, which the page replaces in place.
    # So one turn runs and the replay still shows one message.
    stub_app.conversations = _IdempotentConversations()
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "ship it", "client_message_id": "abc-123_XY"}

    first = await handler(build_request(json_body=body, token=SESSION_TOKEN))
    retry = await handler(build_request(json_body=body, token=SESSION_TOKEN))

    # ONE turn behind two POSTs, and the retry answers with the first attempt's id.
    assert stub_app.conversations.turns == {
        ("web", stub_app.conversations.accept_calls[0]["provider_message_id"]): "turn-1"
    }
    assert _body(first) == _body(retry) == {"data": {"message_id": "turn-1"}}
    entries = [json.loads(entry[1]["data"]) for entry in registered_session.streams[_TRANSCRIPT_KEY]]
    assert [entry["id"] for entry in entries] == ["turn-1", "turn-1"]
    # And each frame carries the visitor's own key, so the page can retire the
    # optimistic bubble it drew for the attempt whose response never arrived.
    assert [entry["client_message_id"] for entry in entries] == ["abc-123_XY", "abc-123_XY"]


async def test_two_messages_without_a_retry_key_are_two_turns(web_env, stub_app, registered_session: FakeRedis):
    # The other side of the same contract: with no key every POST mints a fresh dedup
    # id, so a visitor who really does send the same text twice gets two turns and two
    # bubbles rather than one silently swallowed message.
    stub_app.conversations = _IdempotentConversations()
    handler = _handler(stub_app, _MESSAGES)
    body = {"identity": IDENTITY, "text": "ship it"}

    first = await handler(build_request(json_body=body, token=SESSION_TOKEN))
    second = await handler(build_request(json_body=body, token=SESSION_TOKEN))

    assert len(stub_app.conversations.turns) == 2
    assert _body(first) == {"data": {"message_id": "turn-1"}}
    assert _body(second) == {"data": {"message_id": "turn-2"}}
    entries = [json.loads(entry[1]["data"]) for entry in registered_session.streams[_TRANSCRIPT_KEY]]
    assert [entry["id"] for entry in entries] == ["turn-1", "turn-2"]


async def test_a_message_without_a_retry_key_echoes_none(web_env, stub_app, registered_session: FakeRedis):
    await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    payload = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert "client_message_id" not in payload


@pytest.mark.parametrize("key", ["short", "a" * 65, "has spaces", "bad/char!!", 17])
async def test_messages_reject_a_malformed_retry_key(web_env, stub_app, registered_session: FakeRedis, key: object):
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x", "client_message_id": key}, token=SESSION_TOKEN)
    )
    assert resp.status_code == 422
    assert "client_message_id" in _body(resp)["error"]
    assert stub_app.conversations.accept_calls == []


async def test_a_shed_reply_can_never_precede_the_message_that_caused_it(
    web_env, stub_app, registered_session: FakeRedis
):
    # A rate-shed message's slow-down reply is spawned INSIDE accept, so without the
    # conversation's write-order gate it XADDs before the visitor's own frame and the
    # page replays the agent answering a message that is not there yet.
    spawned: list[asyncio.Task[list[str]]] = []

    class _SheddingConversations:
        def __init__(self) -> None:
            self.accept_calls: list[dict[str, Any]] = []

        async def accept(self, **kwargs: Any) -> str:
            task = asyncio.create_task(WebChannel().notify(make_notification(message="Slow down.")))
            spawned.append(task)
            # Let the spawned reply run until it needs the transcript.
            await asyncio.sleep(0)
            return "turn-shed"

    stub_app.conversations = _SheddingConversations()
    resp = await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "ship it"}, token=SESSION_TOKEN)
    )
    await asyncio.gather(*spawned)

    assert resp.status_code == 200
    texts = [json.loads(entry[1]["data"])["text"] for entry in registered_session.streams[_TRANSCRIPT_KEY]]
    assert texts == ["ship it", "Slow down."]


async def test_messages_unexpected_error_propagates(web_env, stub_app, registered_session: FakeRedis):
    stub_app.conversations.accept_error = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        await _handler(stub_app, _MESSAGES)(
            build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
        )


# -- GET /stream ----------------------------------------------------------------


def _stream_request(query: str = f"identity={IDENTITY}", **kwargs):
    return build_request(method="GET", path=_STREAM, query=query, **kwargs)


async def test_stream_returns_streaming_response(web_env, stub_app, registered_session: FakeRedis):
    from tai42_channel_web.store import append_message

    await append_message(IDENTITY, VISITOR_ID, "out", "already said")
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))
    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["x-content-type-options"] == "nosniff"
    # Draining the body releases the slot the door acquired — and the stream is the
    # caller's OWN transcript, replayed before the backlog marker.
    frames = [str(frame) async for frame in resp.body_iterator]
    assert frames[0].startswith("event: chat.message\ndata: ")
    assert '"text": "already said"' in frames[0]
    assert frames[1] == "event: chat.backlog_done\ndata: {}\n\n"


async def test_stream_without_a_session_cookie_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _STREAM)(_stream_request())
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"


async def test_stream_with_an_unregistered_token_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))
    assert resp.status_code == 401


async def test_stream_without_an_identity_says_it_is_missing(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _STREAM)(_stream_request(query="", token=SESSION_TOKEN))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "the 'identity' query parameter is required"


@pytest.mark.parametrize("query", ["identity=%20%20", "identity=site%3Aalpha", f"identity={'a' * 257}"])
async def test_stream_with_an_unusable_identity_says_it_is_unusable(
    web_env, stub_app, registered_session: FakeRedis, query: str
):
    # A supplied-but-unusable identity is not a missing one; telling a caller their
    # parameter is absent when they sent it sends them looking in the wrong place.
    resp = await _handler(stub_app, _STREAM)(_stream_request(query=query, token=SESSION_TOKEN))
    assert resp.status_code == 400
    assert "must be a non-blank" in _body(resp)["error"]


async def test_stream_unconfigured_store_501(no_web_env, stub_app):
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))
    assert resp.status_code == 501
    assert _body(resp)["code"] == "web_transcript_store_off"


async def test_stream_over_the_visitor_cap_is_503(web_env, stub_app, registered_session: FakeRedis, monkeypatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_MAX_STREAMS_PER_VISITOR", "1")
    reset_all_settings()
    handler = _handler(stub_app, _STREAM)
    first = await handler(_stream_request(token=SESSION_TOKEN))
    assert isinstance(first, StreamingResponse)

    # The door only CHECKS: the slot is taken by the generator, so the first stream
    # holds one only once its body is running.
    assert [frame async for frame in first.body_iterator] == ["event: chat.backlog_done\ndata: {}\n\n"]
    second = await handler(_stream_request(token=SESSION_TOKEN))
    assert isinstance(second, StreamingResponse)
    frames = second.body_iterator.__aiter__()
    assert "backlog_done" in str(await frames.__anext__())

    refused = await handler(_stream_request(token=SESSION_TOKEN))

    assert refused.status_code == 503
    assert _body(refused)["error"] == "too many open chat streams for this session; close one and try again"
    assert refused.headers["cache-control"] == "no-store"
    # The refusal must not have consumed the slot the live stream still holds.
    await _close_body(second)


async def test_a_stream_the_client_abandons_at_the_response_start_leaks_no_slot(
    web_env, stub_app, registered_session: FakeRedis
):
    # The real repro: on ASGI spec_version < 2.4 Starlette races the body against a
    # disconnect listener, and a client gone by then cancels the group at the
    # ``http.response.start`` send — the body generator is never advanced, so its
    # ``finally`` never runs. A slot taken at the door would be pinned for the life of
    # the process, four aborted opens locking a visitor out for their whole session.
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))
    assert isinstance(resp, StreamingResponse)
    sent: list[Any] = []

    async def send(message: Any) -> None:
        # A real send is I/O; suspending here is what lets the cancellation the
        # disconnect listener raised reach the body before its first frame.
        await asyncio.sleep(0)
        sent.append(message)

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    await resp({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "method": "GET"}, receive, send)

    assert sent == [], "the body never ran, so nothing was sent"
    assert stream_module._open_streams == {}


# -- POST /questions/{id}/answer ------------------------------------------------


def _deadline(seconds: float = 300) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def _seed_question(
    interaction_id: str = "int-1",
    callback_url: str = CALLBACK,
    address: str = VISITOR_ID,
    identity: str = IDENTITY,
) -> QuestionRecord:
    record = QuestionRecord(callback_url=callback_url, identity=identity, address=address, timeout_at=_deadline())
    await reserve_question(interaction_id, record)
    return record


def _answer_request(
    interaction_id: str = "int-1",
    answer=None,
    raw_body: bytes | None = None,
    token: str | None = SESSION_TOKEN,
    **kwargs,
):
    return build_request(
        path=_ANSWER.format(interaction_id=interaction_id),
        json_body=None if raw_body is not None else {"answer": answer},
        raw_body=raw_body,
        path_params={"interaction_id": interaction_id},
        token=token,
        **kwargs,
    )


async def test_answer_forwards_and_appends_answered(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="staging"))

    assert resp.status_code == 200
    assert _body(resp) == {"data": {"status": "answered"}}
    assert fake_httpx.calls[0] == {"url": CALLBACK, "json": {"answer": "staging"}, "headers": None}
    answered = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert answered["interaction_id"] == "int-1"
    assert answered["answer"] == "staging"


async def test_answer_without_a_session_cookie_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x", token=None))
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"


async def test_answer_with_an_unregistered_token_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 401


async def test_answer_cross_origin_is_403(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x", extra_headers=_FOREIGN_ORIGIN))
    assert resp.status_code == 403
    assert _body(resp)["code"] == "origin_mismatch"


async def test_answer_unconfigured_store_is_501(no_web_env, stub_app):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 501


async def test_answer_from_another_conversation_is_404_and_keeps_the_record(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # A question is answerable only from the conversation it was asked in — and the
    # refusal never reveals that it exists, nor consumes it.
    await _seed_question(address="other-visitor-id")
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 404
    assert "question not found" in _body(resp)["error"]
    assert "channel:web:question:int-1" in registered_session.store
    assert fake_httpx.calls == []


async def test_answer_invalid_json(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(raw_body=b"{bad"))
    assert resp.status_code == 400


async def test_answer_oversized_body_is_413(web_env, stub_app, registered_session: FakeRedis, monkeypatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_MAX_BODY_BYTES", "64")
    reset_all_settings()
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x" * 500))
    assert resp.status_code == 413


async def test_answer_missing_answer_key(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(raw_body=json.dumps({"nope": 1}).encode()))
    assert resp.status_code == 400


@pytest.mark.parametrize("answer", [["a", "b"], None])
async def test_answer_rejects_a_non_scalar_non_object_value(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, answer
):
    # A web answer is one scalar (text/confirm/select) or a form object; a list or a
    # bare null is neither, and the record is never claimed for a value the door will
    # not forward.
    await _seed_question()
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer=answer))
    assert resp.status_code == 422
    assert fake_httpx.calls == []
    assert "channel:web:question:int-1" in registered_session.store


async def test_answer_forwards_a_form_object(web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx):
    # A form answer is a JSON object; it is forwarded as-is and persisted into the
    # chat.answered frame. The callback door validates it against the stored schema —
    # this door only bounds its shape and size.
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))
    answer = {"name": "Ada", "count": 3}

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer=answer))

    assert resp.status_code == 200
    assert fake_httpx.calls[0]["json"] == {"answer": answer}
    answered = json.loads(registered_session.streams[_TRANSCRIPT_KEY][0][1]["data"])
    assert answered["answer"] == answer


async def test_answer_rejects_an_oversized_object(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # An object whose serialized size exceeds the object cap is a precise 422 (it fits
    # under the body cap), never forwarded, and the record is kept.
    await _seed_question()
    answer = {"blob": "x" * (33 * 1024)}
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer=answer))
    assert resp.status_code == 422
    assert "serialize to at most" in _body(resp)["error"]
    assert fake_httpx.calls == []
    assert "channel:web:question:int-1" in registered_session.store


async def test_answer_rejects_a_non_finite_number_inside_an_object(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # ``json.loads`` accepts ``Infinity`` nested in an object; forwarding it would emit
    # invalid JSON to the callback door AND persist a bad token into the transcript.
    await _seed_question()
    resp = await _handler(stub_app, _ANSWER)(_answer_request(raw_body=b'{"answer": {"x": Infinity}}'))
    assert resp.status_code == 422
    assert _body(resp)["error"] == "answer object must contain only finite numbers"
    assert fake_httpx.calls == []
    assert _TRANSCRIPT_KEY not in registered_session.streams
    assert "channel:web:question:int-1" in registered_session.store


@pytest.mark.parametrize("raw", [b'{"answer": 1e999}', b'{"answer": Infinity}', b'{"answer": NaN}'])
async def test_answer_rejects_a_non_finite_number(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, raw: bytes
):
    # ``json.loads`` accepts these; forwarding one emits invalid JSON to the callback
    # door AND persists an ``Infinity`` token into the transcript, which then fails the
    # page's own JSON parse on every reconnect for the whole transcript TTL.
    await _seed_question()
    resp = await _handler(stub_app, _ANSWER)(_answer_request(raw_body=raw))

    assert resp.status_code == 422
    assert _body(resp)["error"] == "answer must be a finite number"
    assert fake_httpx.calls == []
    assert _TRANSCRIPT_KEY not in registered_session.streams
    assert "channel:web:question:int-1" in registered_session.store


async def test_answer_rejects_an_over_long_string(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x" * 8001))
    assert resp.status_code == 422
    assert "8000 characters" in _body(resp)["error"]


@pytest.mark.parametrize("answer", ["staging", True, 3, 1.5])
async def test_answer_forwards_every_scalar_shape(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, answer
):
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer=answer))
    assert resp.status_code == 200
    assert fake_httpx.calls[0]["json"] == {"answer": answer}


async def test_answer_unknown_question_404(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 404


async def test_answer_claim_lost_to_a_duplicate_is_404(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # The ownership peek passed, then a concurrent duplicate POST claimed the record
    # first: the loser answers 404 rather than forwarding a claimless answer.
    await _seed_question()

    async def _claimed_elsewhere(key: str) -> None:
        return None

    monkeypatch.setattr(registered_session, "getdel", _claimed_elsewhere)
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 404
    assert fake_httpx.calls == []


async def test_answer_door_reports_already_answered_without_a_false_frame(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # The callback door's lost-claim reply is an idempotent 200, never a 409. The
    # recorded answer is someone else's, so no chat.answered frame is written — it
    # would show the visitor a value that was never recorded — and the record stays
    # dropped.
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "already_answered"}}))

    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="mine"))

    assert resp.status_code == 409
    assert _body(resp)["error"] == "that question was already answered"
    assert _TRANSCRIPT_KEY not in registered_session.streams
    assert "channel:web:question:int-1" not in registered_session.store


async def test_answer_door_404_is_terminal_not_restored(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(404, json={"error": "gone"}))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 404
    assert "expired or withdrawn" in _body(resp)["error"]
    assert "channel:web:question:int-1" not in registered_session.store


async def test_answer_door_400_restores_and_surfaces_error(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(400, json={"error": "answer does not match a text question"}))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "answer does not match a text question"
    # Kept so the visitor can re-answer.
    assert "channel:web:question:int-1" in registered_session.store


async def test_answer_door_400_non_json_body_is_never_relayed_to_the_visitor(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # A non-JSON body did not come from the door: it is a proxy or WAF page that
    # answered instead, and it names hosts and software an anonymous visitor is not
    # entitled to. The operator gets it in the log; the visitor gets this door's own
    # refusal.
    await _seed_question()
    fake_httpx.responses.append(response(400, text="<h1>BigProxy 4.2 blocked internal-app-07.corp</h1>"))
    with caplog.at_level("WARNING"):
        resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))

    assert resp.status_code == 400
    assert _body(resp)["error"] == "the answer was refused"
    assert "internal-app-07" not in json.dumps(_body(resp))
    assert any("internal-app-07" in r.getMessage() for r in caplog.records)


async def test_answer_door_400_json_without_the_error_envelope_is_never_relayed(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(400, json={"detail": "no error key"}))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "the answer was refused"


async def test_a_refused_answer_is_only_restored_so_many_times(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx, monkeypatch, caplog
):
    # A visitor looping an out-of-enum answer would otherwise forward for as long as
    # the question lives, draining that shared callback bucket for every channel.
    monkeypatch.setenv("CHANNEL_WEB_MAX_ANSWER_RESTORES", "2")
    reset_all_settings()
    await _seed_question()
    handler = _handler(stub_app, _ANSWER)
    for _ in range(3):
        fake_httpx.responses.append(response(400, json={"error": "answer does not match a select question"}))

    for _ in range(2):
        refused = await handler(_answer_request(answer="nope"))
        assert refused.status_code == 400
        assert _body(refused)["error"] == "answer does not match a select question"

    with caplog.at_level("ERROR"):
        spent = await handler(_answer_request(answer="nope"))

    assert spent.status_code == 400
    assert _body(spent)["error"].endswith("; that question can no longer be answered")
    # The record is left dropped — a sane state: the ask resolves by its own timeout.
    assert "channel:web:question:int-1" not in registered_session.store
    assert any("already been refused" in r.getMessage() for r in caplog.records)
    # And the loop stops there: the next attempt never reaches the callback door.
    assert (await handler(_answer_request(answer="nope"))).status_code == 404
    assert len(fake_httpx.calls) == 3


async def test_the_answered_frame_takes_the_write_order_gate(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # Every agent-side append takes the conversation's gate; this one used to be the
    # exception, so the reply an answer sets off could XADD ahead of the frame that
    # settles the question the reply is answering.
    await _seed_question()
    fake_httpx.responses.append(response(200, json={"data": {"status": "answered"}}))
    holder = asyncio.Event()

    async def _hold_the_gate() -> None:
        async with transcript_order(IDENTITY, VISITOR_ID):
            holder.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    held = asyncio.create_task(_hold_the_gate())
    await holder.wait()
    answering = asyncio.ensure_future(_handler(stub_app, _ANSWER)(_answer_request(answer="x")))
    await asyncio.sleep(0)
    # The append cannot land while another writer holds the conversation's gate.
    assert _TRANSCRIPT_KEY not in registered_session.streams

    await held
    assert (await answering).status_code == 200
    assert _TRANSCRIPT_KEY in registered_session.streams


async def test_answer_non_json_2xx_still_appends(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    # A 2xx whose body is not the envelope is an accepted answer, not a lost claim.
    await _seed_question()
    fake_httpx.responses.append(response(200, text="ok"))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 200
    assert _TRANSCRIPT_KEY in registered_session.streams


async def test_answer_2xx_envelope_without_status_still_appends(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(200, json=["not", "an", "envelope"]))
    resp = await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert resp.status_code == 200


async def test_answer_door_other_status_restores_and_raises(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(response(500, text="upstream boom"))
    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert "channel:web:question:int-1" in registered_session.store


async def test_answer_transport_failure_restores_and_raises(
    web_env, stub_app, registered_session: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_question()
    fake_httpx.responses.append(httpx.ConnectError("no route to host"))
    with pytest.raises(httpx.HTTPError):
        await _handler(stub_app, _ANSWER)(_answer_request(answer="x"))
    assert "channel:web:question:int-1" in registered_session.store


# -- POST /session/rotate -------------------------------------------------------


async def test_rotate_unregisters_the_old_session_and_mints_a_fresh_one(
    web_env, stub_app, registered_session: FakeRedis
):
    resp = await _handler(stub_app, _ROTATE)(_rotate_request(token=SESSION_TOKEN))

    assert resp.status_code == 200
    assert _body(resp) == {"data": {"status": "rotated"}}
    minted = _set_cookie(resp)[SECURE_COOKIE]
    assert minted.value != SESSION_TOKEN
    assert minted["httponly"] is True
    # The old token can never address the old conversation again…
    assert await resolve_session(SESSION_TOKEN) is None
    # …and the new one addresses a brand-new, empty conversation on the same route.
    rotated = await resolve_session(minted.value)
    assert rotated is not None
    assert rotated.visitor_id != VISITOR_ID
    assert rotated.identity == IDENTITY


async def test_rotate_without_a_session_still_mints_one(web_env, stub_app, fake_redis: FakeRedis):
    # Rotation is not a credential check: opening the chat page mints a session to
    # anyone, so requiring one here would refuse what the page hands out for free.
    resp = await _handler(stub_app, _ROTATE)(_rotate_request())
    assert resp.status_code == 200
    assert await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value) is not None


async def test_rotate_cross_origin_is_403(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ROTATE)(_rotate_request(token=SESSION_TOKEN, extra_headers=_FOREIGN_ORIGIN))
    assert resp.status_code == 403
    assert _body(resp)["code"] == "origin_mismatch"
    registration = await resolve_session(SESSION_TOKEN)
    assert registration is not None
    assert registration.visitor_id == VISITOR_ID


async def test_rotate_with_an_unreadable_body_is_400(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _ROTATE)(build_request(path=_ROTATE, raw_body=b"{bad", token=SESSION_TOKEN))

    assert resp.status_code == 400
    assert _body(resp)["error"] == "invalid JSON body"
    # Nothing was rotated: the session it was asked to replace still resolves.
    assert await resolve_session(SESSION_TOKEN) is not None


async def test_rotate_unconfigured_store_is_501(no_web_env, stub_app):
    # The store guard stands ahead of the body: without a registration store there is
    # nothing to mint into, whatever the body says.
    resp = await _handler(stub_app, _ROTATE)(build_request(path=_ROTATE, token=SESSION_TOKEN))
    assert resp.status_code == 501


# -- link params capture (chat page door) ---------------------------------------


async def test_chat_page_captures_link_params_at_mint(web_env, stub_app, fake_redis: FakeRedis, public_build: Path):
    resp = await _handler(stub_app, _CHAT)(_chat_request(query="ref=spring&n=3"))
    assert resp.status_code == 200
    registration = await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value)
    assert registration is not None
    assert registration.params == {"ref": "spring", "n": "3"}


async def test_a_fresh_mint_with_no_params_stores_the_empty_map(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    resp = await _handler(stub_app, _CHAT)(_chat_request())
    registration = await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value)
    assert registration is not None
    assert registration.params == {}


async def test_reserved_query_names_are_stripped_never_stored(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    # ``tai_pair`` (client-consumed) and ``tai_entry`` (gate code) never become params.
    resp = await _handler(stub_app, _CHAT)(_chat_request(query="tai_pair=LINK-ABCD1234&tai_entry=code&ref=spring"))
    registration = await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value)
    assert registration is not None
    assert registration.params == {"ref": "spring"}


async def test_a_navigation_with_new_params_rewrites_a_live_session(
    web_env, stub_app, registered_session: FakeRedis, public_build: Path
):
    resp = await _handler(stub_app, _CHAT)(
        _chat_request(token=SESSION_TOKEN, query="ref=summer", extra_headers=_NAVIGATION)
    )
    assert resp.status_code == 200
    assert _set_cookie(resp)[SECURE_COOKIE].value == SESSION_TOKEN
    registration = await resolve_session(SESSION_TOKEN)
    assert registration is not None
    assert registration.params == {"ref": "summer"}


async def test_a_navigation_without_params_preserves_stored_params(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY, {"ref": "keep"})
    resp = await _handler(stub_app, _CHAT)(_chat_request(token=SESSION_TOKEN, extra_headers=_NAVIGATION))
    assert resp.status_code == 200
    registration = await resolve_session(SESSION_TOKEN)
    assert registration is not None
    assert registration.params == {"ref": "keep"}


async def test_a_subresource_with_params_never_rewrites_a_live_session(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    # A cross-site subresource GET must not overwrite a live visitor's params — only a
    # top-level NAVIGATION rewrites them.
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY, {"ref": "keep"})
    resp = await _handler(stub_app, _CHAT)(
        _chat_request(token=SESSION_TOKEN, query="ref=evil", extra_headers=_SUBRESOURCE)
    )
    assert resp.status_code == 200
    registration = await resolve_session(SESSION_TOKEN)
    assert registration is not None
    assert registration.params == {"ref": "keep"}


async def test_a_rotation_mints_a_clean_registration_with_no_params(web_env, stub_app, fake_redis: FakeRedis):
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY, {"ref": "old"})
    resp = await _handler(stub_app, _ROTATE)(_rotate_request(token=SESSION_TOKEN))
    registration = await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value)
    assert registration is not None
    assert registration.params == {}


@pytest.mark.parametrize(
    "query",
    [
        "tai_pair=a&tai_pair=b",  # a duplicated RESERVED name is a bound violation too
        "tai_entry=x&tai_entry=y",
        "a=1&a=2",  # duplicated non-reserved key
        "bad key=1",  # key regex: a space
        "a" * 65 + "=1",  # key regex: over 64 chars
        "k=" + "x" * 513,  # value over 512 chars
        "&".join(f"k{i}=1" for i in range(17)),  # over 16 keys
    ],
)
async def test_link_params_bound_violations_are_a_400_page(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path, query: str
):
    resp = await _handler(stub_app, _CHAT)(_chat_request(query=query))
    assert resp.status_code == 400
    _refusal(resp, "link_params_invalid")
    # Nothing was minted: a refused entry establishes no session.
    assert fake_redis.store == {}


async def test_messages_pass_captured_params_to_accept(web_env, stub_app, fake_redis: FakeRedis):
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY, {"ref": "spring"})
    await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert stub_app.conversations.accept_calls[0]["params"] == {"ref": "spring"}


async def test_messages_pass_none_when_no_params(web_env, stub_app, registered_session: FakeRedis):
    # Empty params -> None -> the tool payload is byte-identical to today.
    await _handler(stub_app, _MESSAGES)(
        build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
    )
    assert stub_app.conversations.accept_calls[0]["params"] is None


# -- old-shape session records fail loud, the door re-mints ----------------------


def _seed_old_shape_record(fake: FakeRedis) -> None:
    """A record predating link params (no ``params`` key) — the strict decode refuses
    it; the DOOR re-mints. Hand-written, not via ``register`` (which writes the new
    shape)."""
    fake.store[_SESSION_KEY] = json.dumps({"visitor_id": VISITOR_ID, "identity": IDENTITY, "created_at": "now"})


async def test_page_door_re_mints_over_an_old_shape_record(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path, caplog: pytest.LogCaptureFixture
):
    _seed_old_shape_record(fake_redis)
    with caplog.at_level("WARNING"):
        resp = await _handler(stub_app, _CHAT)(_chat_request(token=SESSION_TOKEN))
    # No 500: a fresh session under a NEW token, and the dead record is orphaned (not
    # overwritten) so it ages out on its own.
    assert resp.status_code == 200
    minted = _set_cookie(resp)[SECURE_COOKIE].value
    assert minted != SESSION_TOKEN
    registration = await resolve_session(minted)
    assert registration is not None
    assert registration.params == {}
    assert any("record was refused" in record.getMessage() for record in caplog.records)


async def test_message_door_takes_the_no_session_refusal_over_an_old_shape_record(
    web_env, stub_app, fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
):
    _seed_old_shape_record(fake_redis)
    with caplog.at_level("WARNING"):
        resp = await _handler(stub_app, _MESSAGES)(
            build_request(json_body={"identity": IDENTITY, "text": "x"}, token=SESSION_TOKEN)
        )
    # The shared session helper catches the decode error and the door takes its normal
    # no-session 401 — never a 500.
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"
    assert stub_app.conversations.accept_calls == []


# -- entry gate (chat page door) ------------------------------------------------


async def test_gated_route_admits_a_navigation_with_a_valid_code(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    await set_gate(IDENTITY, True)
    raw_code, _ = await mint_entry_code(IDENTITY, None, None)
    resp = await _handler(stub_app, _CHAT)(_chat_request(query=f"tai_entry={raw_code}"))
    assert resp.status_code == 200
    # A valid code consumes nothing — codes are multi-use.
    assert await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value) is not None


async def test_gated_route_refuses_missing_and_wrong_codes_identically(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    await set_gate(IDENTITY, True)
    handler = _handler(stub_app, _CHAT)
    missing = await handler(_chat_request())
    wrong = await handler(_chat_request(query="tai_entry=nope"))
    assert missing.status_code == wrong.status_code == 403
    _refusal(missing, "entry_refused")
    # ONE page, ONE wording — the same bytes for both (no oracle).
    assert bytes(missing.body) == bytes(wrong.body)
    assert missing.headers.items() == wrong.headers.items()


async def test_gated_route_refuses_an_expired_code(web_env, stub_app, fake_redis: FakeRedis, public_build: Path):
    await set_gate(IDENTITY, True)
    raw_code, code_id = await mint_entry_code(IDENTITY, None, datetime.now(UTC) + timedelta(hours=1))
    # The TTL lapsing IS the key vanishing.
    del fake_redis.store[f"channel:web:entry_code:{IDENTITY}:{code_id}"]
    resp = await _handler(stub_app, _CHAT)(_chat_request(query=f"tai_entry={raw_code}"))
    assert resp.status_code == 403
    _refusal(resp, "entry_refused")


async def test_gated_route_refuses_a_revoked_code(web_env, stub_app, fake_redis: FakeRedis, public_build: Path):
    await set_gate(IDENTITY, True)
    raw_code, code_id = await mint_entry_code(IDENTITY, None, None)
    await revoke_entry_code(IDENTITY, code_id)
    resp = await _handler(stub_app, _CHAT)(_chat_request(query=f"tai_entry={raw_code}"))
    assert resp.status_code == 403
    _refusal(resp, "entry_refused")


async def test_gated_route_refuses_once_the_guess_throttle_is_spent(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    await set_gate(IDENTITY, True)
    handler = _handler(stub_app, _CHAT)
    # The default cap is 10 guesses per window, all from one client bucket.
    for _ in range(10):
        assert (await handler(_chat_request(query="tai_entry=nope"))).status_code == 403
    # Now even a VALID code is refused — the bucket is spent (throttle checked FIRST).
    raw_code, _ = await mint_entry_code(IDENTITY, None, None)
    resp = await handler(_chat_request(query=f"tai_entry={raw_code}"))
    assert resp.status_code == 403
    _refusal(resp, "entry_refused")


async def test_non_navigation_on_a_gated_route_ignores_the_code(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    # The navigation guard runs BEFORE the gate check: a subresource answers
    # ``not_a_navigation`` with a VALID and with an INVALID code alike, so the response
    # never differs by code validity (no oracle).
    await set_gate(IDENTITY, True)
    raw_code, _ = await mint_entry_code(IDENTITY, None, None)
    handler = _handler(stub_app, _CHAT)
    valid = await handler(_chat_request(query=f"tai_entry={raw_code}", extra_headers=_SUBRESOURCE))
    invalid = await handler(_chat_request(query="tai_entry=nope", extra_headers=_SUBRESOURCE))
    assert valid.status_code == invalid.status_code == 403
    _refusal(valid, "not_a_navigation")
    assert bytes(valid.body) == bytes(invalid.body)
    assert valid.headers.items() == invalid.headers.items()


async def test_a_live_session_bypasses_the_gate(web_env, stub_app, registered_session: FakeRedis, public_build: Path):
    # A session is an already-granted capability: it admits without a code.
    await set_gate(IDENTITY, True)
    resp = await _handler(stub_app, _CHAT)(_chat_request(token=SESSION_TOKEN))
    assert resp.status_code == 200
    assert _set_cookie(resp)[SECURE_COOKIE].value == SESSION_TOKEN


async def test_page_and_refusals_carry_no_referrer(web_env, stub_app, fake_redis: FakeRedis, public_build: Path):
    # A capability URL must never leak via referrer.
    ok = await _handler(stub_app, _CHAT)(_chat_request())
    assert ok.headers["referrer-policy"] == "no-referrer"
    await set_gate(IDENTITY, True)
    refused = await _handler(stub_app, _CHAT)(_chat_request())
    assert refused.headers["referrer-policy"] == "no-referrer"


async def test_page_and_rotate_refusals_share_the_entry_wording(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    from tai42_channel_web.routes import _ENTRY_REFUSED_MESSAGE

    await set_gate(IDENTITY, True)
    page_resp = await _handler(stub_app, _CHAT)(_chat_request())
    rotate_resp = await _handler(stub_app, _ROTATE)(_rotate_request())
    assert _ENTRY_REFUSED_MESSAGE in bytes(page_resp.body).decode()
    assert _body(rotate_resp)["error"] == _ENTRY_REFUSED_MESSAGE
    assert _body(rotate_resp)["code"] == "entry_refused"


# -- entry gate (rotate door) ---------------------------------------------------


async def test_rotate_on_a_gated_route_refuses_without_a_live_code(web_env, stub_app, fake_redis: FakeRedis):
    await set_gate(IDENTITY, True)
    handler = _handler(stub_app, _ROTATE)
    missing = await handler(_rotate_request())
    wrong = await handler(build_request(path=_ROTATE, json_body={"identity": IDENTITY, "entry_code": "nope"}))
    assert missing.status_code == wrong.status_code == 403
    assert _body(missing)["code"] == _body(wrong)["code"] == "entry_refused"


async def test_rotate_on_a_gated_route_admits_a_live_code_and_clears_params(web_env, stub_app, fake_redis: FakeRedis):
    await set_gate(IDENTITY, True)
    raw_code, _ = await mint_entry_code(IDENTITY, None, None)
    resp = await _handler(stub_app, _ROTATE)(
        build_request(path=_ROTATE, json_body={"identity": IDENTITY, "entry_code": raw_code})
    )
    assert resp.status_code == 200
    registration = await resolve_session(_set_cookie(resp)[SECURE_COOKIE].value)
    assert registration is not None
    assert registration.params == {}


async def test_rotate_on_an_ungated_route_ignores_the_code_field(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _ROTATE)(
        build_request(path=_ROTATE, json_body={"identity": IDENTITY, "entry_code": "irrelevant"})
    )
    assert resp.status_code == 200


# -- management doors (authed) --------------------------------------------------

_GATES = "/api/channels/web/gates/{identity}"
_CODES = "/api/channels/web/gates/{identity}/codes"
_CODE = "/api/channels/web/gates/{identity}/codes/{code_id}"


def _managed_route(stub_app, path: str, method: str):
    routes = [route for route in stub_app.http.routes if route.path == path and method in route.methods]
    assert len(routes) == 1
    return routes[0]


def _gate_request(method: str, path: str, *, json_body=None, path_params: dict[str, str]):
    return build_request(method=method, path=path, json_body=json_body, path_params=path_params)


def test_management_doors_are_authed_with_the_pinned_action_class(stub_app):
    # An authed route with no action-class refuses to register in the core registry
    # (fail-closed); the stub records the declared metadata so the pin is asserted here.
    read = _managed_route(stub_app, _GATES, "GET")
    assert read.authed is True
    assert read.action == "read"
    for path, method in [(_GATES, "PUT"), (_CODES, "POST"), (_CODE, "DELETE")]:
        route = _managed_route(stub_app, path, method)
        assert route.authed is True
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
