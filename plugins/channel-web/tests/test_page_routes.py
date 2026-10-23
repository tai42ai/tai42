"""The chat-page navigation doors — the built shell, session mint/refresh and its
navigation guard, asset serving, link-param capture, the old-shape re-mint, and the
page-door entry gate."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from starlette.responses import FileResponse

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.page import PAGE_CSP
from tai42_channel_web.routes.entry_admission import _ENTRY_REFUSED_MESSAGE
from tai42_channel_web.store.entry_gate import mint_entry_code, revoke_entry_code, set_gate
from tai42_channel_web.store.registrations import resolve_session
from tai42_channel_web.store.words import set_words
from tai42_channel_web.words import WebWords

from .conftest import (
    _ASSETS,
    _ASSETS_URL,
    _CHAT,
    _NAVIGATION,
    _ROTATE,
    _SESSION_KEY,
    _SUBRESOURCE,
    ENTRY_ASSET,
    IDENTITY,
    OTHER_IDENTITY,
    PLAIN_COOKIE,
    SECURE_COOKIE,
    SESSION_TOKEN,
    STYLE_ASSET,
    VISITOR_ID,
    FakeRedis,
    _body,
    _chat_request,
    _handler,
    _refusal,
    _rotate_request,
    _seed_old_shape_record,
    _sent_body,
    _set_cookie,
    build_request,
    register,
    write_manifest,
)

# -- GET /chat/{identity} -------------------------------------------------------


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
    # The mount the door served under rides #root's data-api-base, so the bundle's
    # own API calls follow the actual mount rather than a hardcoded default.
    assert 'data-api-base="/api/channels/web"' in html
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


# -- GET /assets/{file} ---------------------------------------------------------


def _asset_request(name: str):
    return build_request(method="GET", path=_ASSETS_URL.format(file=name), path_params={"file": name})


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


# -- old-shape session records fail loud, the door re-mints ----------------------


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
    await set_gate(IDENTITY, True)
    page_resp = await _handler(stub_app, _CHAT)(_chat_request())
    rotate_resp = await _handler(stub_app, _ROTATE)(_rotate_request())
    assert _ENTRY_REFUSED_MESSAGE in bytes(page_resp.body).decode()
    assert _body(rotate_resp)["error"] == _ENTRY_REFUSED_MESSAGE
    assert _body(rotate_resp)["code"] == "entry_refused"


# -- the identity's own refusal-page words and transport sentences ---------------

_PAGE_WORDS = {
    "not_a_navigation": {"heading": "Open it directly", "text": "Use the chat link in a new tab."},
    "link_invalid": {"heading": "Broken link", "text": "Ask for a fresh chat link."},
    "entry_refused": {"heading": "Members only", "text": "Use the link from your invitation."},
    "page_unavailable": {"heading": "Down for now", "text": "Come back in a few minutes."},
}


async def _store_page_words(identity: str = IDENTITY) -> None:
    await set_words(identity, WebWords.model_validate({"page": _PAGE_WORDS}))


async def _refusal_for(stub_app, case: str, identity: str, tmp_path: Path, monkeypatch) -> Any:
    handler = _handler(stub_app, _CHAT)
    if case == "link_invalid":
        return await handler(_chat_request(identity=identity, query="a=1&a=2"))
    if case == "not_a_navigation":
        return await handler(_chat_request(identity=identity, extra_headers=_SUBRESOURCE))
    if case == "entry_refused":
        await set_gate(identity, True)
        return await handler(_chat_request(identity=identity))
    from tai42_channel_web import page

    monkeypatch.setattr(page, "_public_dir", lambda: tmp_path / "absent")
    return await handler(_chat_request(identity=identity))


_CASES = [
    ("link_invalid", 400, "link_params_invalid"),
    ("not_a_navigation", 403, "not_a_navigation"),
    ("entry_refused", 403, "entry_refused"),
    ("page_unavailable", 500, None),
]


@pytest.mark.parametrize(("case", "status", "code"), _CASES)
async def test_each_refusal_page_shows_the_identity_words(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path, tmp_path: Path, monkeypatch, case, status, code
):
    await _store_page_words()
    resp = await _refusal_for(stub_app, case, IDENTITY, tmp_path, monkeypatch)
    assert resp.status_code == status
    html = _refusal(resp, code)
    words = _PAGE_WORDS[case]
    assert f"<title>{words['heading']}</title>" in html
    assert f"<h1>{words['heading']}</h1>" in html
    assert f"<p>{words['text']}</p>" in html


@pytest.mark.parametrize(("case", "status", "code"), _CASES)
async def test_another_identity_keeps_the_built_in_refusal_pages(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path, tmp_path: Path, monkeypatch, case, status, code
):
    await _store_page_words(OTHER_IDENTITY)
    resp = await _refusal_for(stub_app, case, IDENTITY, tmp_path, monkeypatch)
    assert resp.status_code == status
    html = _refusal(resp, code)
    for words in _PAGE_WORDS.values():
        assert words["heading"] not in html
        assert words["text"] not in html


async def test_a_refusal_page_with_only_a_heading_keeps_the_built_in_text(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    await set_words(IDENTITY, WebWords.model_validate({"page": {"entry_refused": {"heading": "Members only"}}}))
    await set_gate(IDENTITY, True)
    html = _refusal(await _handler(stub_app, _CHAT)(_chat_request()), "entry_refused")
    assert "<h1>Members only</h1>" in html
    assert f"<p>{_ENTRY_REFUSED_MESSAGE}</p>" in html


async def test_the_entry_refusal_with_words_is_still_one_page_for_every_code(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    await _store_page_words()
    await set_gate(IDENTITY, True)
    handler = _handler(stub_app, _CHAT)
    missing = await handler(_chat_request())
    wrong = await handler(_chat_request(query="tai_entry=nope"))
    assert bytes(missing.body) == bytes(wrong.body)


async def test_the_served_page_carries_the_identity_transport_words(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    transport = {"busy": 'Hold on "a" <moment>.', "too_long": "{subject} is over the limit."}
    await set_words(IDENTITY, WebWords.model_validate({"transport": transport, "page": _PAGE_WORDS}))
    html = bytes((await _handler(stub_app, _CHAT)(_chat_request())).body).decode()
    root = next(line for line in html.splitlines() if line.startswith('<div id="root"'))
    raw = root.split('data-words="', 1)[1].split('"', 1)[0]
    assert json.loads(unescape(raw)) == transport
    assert "<moment>" not in html


async def test_the_served_page_carries_no_words_attribute_without_transport_words(
    web_env, stub_app, fake_redis: FakeRedis, public_build: Path
):
    plain = bytes((await _handler(stub_app, _CHAT)(_chat_request())).body).decode()
    assert "data-words" not in plain
    await _store_page_words()
    page_only = bytes((await _handler(stub_app, _CHAT)(_chat_request())).body).decode()
    assert "data-words" not in page_only


async def test_the_store_off_page_shows_the_deployment_words(no_web_env, stub_app, public_build: Path, monkeypatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_STORE_OFF_TITLE", "Closed <today>")
    monkeypatch.setenv("CHANNEL_WEB_STORE_OFF_TEXT", "Come back later.")
    reset_all_settings()
    resp = await _handler(stub_app, _CHAT)(_chat_request())
    assert resp.status_code == 501
    html = _refusal(resp, "web_transcript_store_off")
    assert "<h1>Closed &lt;today&gt;</h1>" in html
    assert "<p>Come back later.</p>" in html
