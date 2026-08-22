"""The house OFF pattern, pinned end to end against one all-features-OFF
deployment.

``off_stack`` mounts the WHOLE default router surface but wires no feature Redis
and no default Postgres, so every DB-backed feature is genuinely unconfigured.
This one module pins the entire doctrine against it: collection reads answer
200-empty, mutations refuse 501 with a machine-readable ``<feature>-not-configured``
code, named-entity reads 404 with a body byte-identical to a genuine miss, the
public callback doors stay a uniform 404 (no OFF oracle), the SSE stream refuses
501 before any body byte, ``/ready`` is 200 with an empty checks map, ``kinds``
carries an ``off`` row per gated feature, and the boot log names the OFF state with
exactly one rate-limit WARNING and the skip INFO lines.

The stack runs no backend, so the module is ``backendless`` — it runs once on the
default backend leg; the OFF surface is backend-invariant.
"""

from __future__ import annotations

import httpx
import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.webchat import SESSION_COOKIE as _WEB_SESSION_COOKIE

pytestmark = pytest.mark.backendless

# The DB-backed features and the machine-readable code each stamps into its 501
# mutation refusal (verified against the skeleton's operations layer).
_TOOL_RUNS_CODE = "tool-runs-not-configured"
_INTERACTIONS_CODE = "interactions-not-configured"
_MARKETPLACE_CODE = "marketplace-not-configured"
_TOOL_META_CODE = "tool-meta-not-configured"
_CONNECTORS_CODE = "connectors-not-configured"
_VERSIONING_CODE = "versioning-not-configured"
# The web chat plugin carries its OWN store gate, so its refusal code is the plugin's
# (``tai42_channel_web.routes``), not a skeleton ``<feature>-not-configured``.
_WEB_STORE_OFF_CODE = "web_transcript_store_off"
_WEB_PAGE_PATH = "/api/channels/web/chat/e2e-off-site"
# The page door answers a browser, so it carries that code in a meta tag instead of a
# JSON field (``tai42_channel_web.page.REFUSAL_CODE_META``).
_WEB_REFUSAL_CODE_META = f'<meta name="tai42-refusal-code" content="{_WEB_STORE_OFF_CODE}">'
# The prefix every bundle link in the served SHELL carries — absent from every refusal
# page, which links nothing at all.
_WEB_ASSET_URL_PREFIX = "/api/channels/web/assets/"

# The gated features that render a ``state="off"`` row in GET /api/system/kinds
# when unconfigured. NOTE the marketplace row's kind is ``marketplace_store`` (the
# install store), not ``marketplace``.
_GATED_OFF_KINDS = frozenset(
    {"tool_runs", "interactions", "rate_limit", "marketplace_store", "tool_meta", "connectors", "versioning"}
)


def _off_api(off_stack: TaiStack) -> ApiClient:
    return off_stack.api()


async def _raw(off_stack: TaiStack, method: str, path: str, **kw) -> httpx.Response:
    return await _off_api(off_stack).request_raw(method, path, **kw)


# ---- 200-empty collection reads -----------------------------------------


async def test_collection_reads_answer_200_empty(off_stack: TaiStack) -> None:
    api = _off_api(off_stack)

    # tool-runs list for a real tool -> the honest empty slice.
    assert await api.get("/api/tool-runs?tool_name=generate_uuid") == []

    # notifications feed.
    assert await api.get("/api/notifications") == {"notifications": []}

    # interactions pending list -> the honest empty page (the paged inbox door).
    assert await api.get("/api/interactions") == {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 50,
        "next_page": None,
        "truncated": False,
    }

    # marketplace installed inventory + advisories.
    assert await api.get("/api/marketplace/installed") == {"installed": [], "quarantined": []}
    advisories = await api.get("/api/marketplace/advisories")
    assert advisories["advisories"] == []

    # tool-metadata overlay.
    assert await api.get("/api/tool-meta") == {"folders": [], "meta": []}

    # connectors connections + the (store-independent) provider catalog.
    assert await api.get("/api/connectors/connections") == {"items": [], "total": 0, "unhealthy": 0}
    assert await api.get("/api/connectors/providers") == {"providers": [], "categories": []}

    # presets list degrades to empty, never 501.
    assert await api.get("/api/presets") == []


# ---- 501 + named-code mutation refusals ----------------------------------


async def _assert_501_code(off_stack: TaiStack, method: str, path: str, code: str, *, json=None) -> None:
    resp = await _raw(off_stack, method, path, json=json)
    assert resp.status_code == 501, f"{method} {path} -> {resp.status_code}; body: {resp.text}"
    body = resp.json()
    assert body.get("code") == code, f"{method} {path} code={body.get('code')!r}; body: {resp.text}"
    # The message is a named, actionable reason, never a bare traceback.
    assert isinstance(body.get("error"), str), resp.text
    assert body["error"], resp.text


async def test_mutations_refuse_501_with_named_code(off_stack: TaiStack) -> None:
    # tool-run submit (a real tool name; the store gate refuses before dispatch).
    await _assert_501_code(
        off_stack, "POST", "/api/tool-runs", _TOOL_RUNS_CODE, json={"tool_name": "generate_uuid", "arguments": {}}
    )

    # tool-meta upsert + folder create.
    await _assert_501_code(
        off_stack, "PATCH", "/api/tool-meta/tools/generate_uuid", _TOOL_META_CODE, json={"display_name": "x"}
    )
    await _assert_501_code(
        off_stack, "POST", "/api/tool-meta/folders", _TOOL_META_CODE, json={"name": "f", "parent_id": None}
    )

    # marketplace install / uninstall / update / upgrade-all — one code for all four.
    await _assert_501_code(off_stack, "POST", "/api/marketplace/install", _MARKETPLACE_CODE, json={"ref": "acme/thing"})
    await _assert_501_code(
        off_stack, "POST", "/api/marketplace/uninstall", _MARKETPLACE_CODE, json={"ref": "acme/thing"}
    )
    await _assert_501_code(off_stack, "POST", "/api/marketplace/update", _MARKETPLACE_CODE, json={"ref": "acme/thing"})
    await _assert_501_code(off_stack, "POST", "/api/marketplace/upgrade-all", _MARKETPLACE_CODE, json={})

    # connectors start.
    await _assert_501_code(
        off_stack,
        "POST",
        "/api/connectors/connections/start",
        _CONNECTORS_CODE,
        json={"provider_id": "ghost", "alias": "a", "enabled_sub_services": ["svc"]},
    )

    # preset write (versioning code — 501, not 503).
    await _assert_501_code(
        off_stack,
        "POST",
        "/api/presets",
        _VERSIONING_CODE,
        json={"name": "p_off", "base_tool": "generate_uuid", "description": "off", "fixed_kwargs": {}},
    )


# ---- 404 named-entity reads, byte-identical to a genuine miss ------------


async def _assert_404_body(off_stack: TaiStack, method: str, path: str, error: str, *, json=None) -> None:
    resp = await _raw(off_stack, method, path, json=json)
    assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}; body: {resp.text}"
    # The store-less miss is byte-identical to a genuine miss: the same handler
    # raises the same NotFoundError string in both branches, so the OFF body carries
    # no distinguishing signal.
    assert resp.json() == {"error": error}, resp.text


async def test_named_reads_are_404_miss_identical(off_stack: TaiStack) -> None:
    # tool-run get by id (single-quoted repr of the id).
    await _assert_404_body(off_stack, "GET", "/api/tool-runs/ghost", "run 'ghost' not found")

    # interactions answer by id.
    await _assert_404_body(
        off_stack, "POST", "/api/interactions/ghost/answer", "Interaction not found", json={"answer": "x"}
    )

    # connectors get / delete / reconnect / sub-services — one miss string.
    await _assert_404_body(off_stack, "GET", "/api/connectors/connections/ghost", "connection not found")
    await _assert_404_body(off_stack, "DELETE", "/api/connectors/connections/ghost", "connection not found")
    await _assert_404_body(
        off_stack,
        "POST",
        "/api/connectors/connections/ghost/reconnect",
        "connection not found",
        json={"enabled_sub_services": ["svc"]},
    )
    await _assert_404_body(
        off_stack,
        "PATCH",
        "/api/connectors/connections/ghost/sub-services",
        "connection not found",
        json={"enabled_sub_services": ["svc"]},
    )

    # tool-meta folder rename / move / delete (single-quoted repr of the id).
    await _assert_404_body(
        off_stack, "POST", "/api/tool-meta/folders/ghost/rename", "folder 'ghost' not found", json={"name": "x"}
    )
    await _assert_404_body(
        off_stack, "POST", "/api/tool-meta/folders/ghost/move", "folder 'ghost' not found", json={"parent_id": None}
    )
    await _assert_404_body(off_stack, "DELETE", "/api/tool-meta/folders/ghost", "folder 'ghost' not found")


# ---- public doors: uniform 404, no OFF oracle ---------------------------


async def test_interactions_callback_doors_are_uniform_404(off_stack: TaiStack) -> None:
    # GET redirect door -> the door's own plaintext 404, indistinguishable from an
    # unknown/expired ticket (never a 501 that would out the OFF state).
    get_resp = await _raw(off_stack, "GET", "/api/interactions/callback/ghost-ticket")
    assert get_resp.status_code == 404, get_resp.text
    assert get_resp.headers["content-type"].startswith("text/plain")
    assert get_resp.text == "Not Found"

    # POST data door -> the door's own JSON 404, same as an unknown ticket.
    post_resp = await _raw(off_stack, "POST", "/api/interactions/callback/ghost-ticket", json={"answer": "x"})
    assert post_resp.status_code == 404, post_resp.text
    assert post_resp.json() == {"error": "not found"}


async def test_interactions_media_door_is_uniform_404(off_stack: TaiStack) -> None:
    # The served-media door answers the SAME 404 as a miss (a well-formed id that is not
    # stored), never a 501 that would out the OFF state on this unauthenticated door.
    resp = await _raw(off_stack, "GET", "/api/interactions/media/" + "a" * 43)
    assert resp.status_code == 404, resp.text
    assert resp.json() == {"error": "not found"}


async def test_sse_stream_refuses_501_before_body(off_stack: TaiStack) -> None:
    # The stream door refuses with a buffered 501 built BEFORE the StreamingResponse,
    # so the body is a complete JSON envelope (the stream never opened) carrying the
    # interactions code — not a half-open event stream.
    resp = await _raw(off_stack, "GET", "/api/interactions/stream")
    assert resp.status_code == 501, resp.text
    body = resp.json()
    assert body.get("code") == _INTERACTIONS_CODE, resp.text


async def test_web_chat_doors_refuse_501_when_the_transcript_store_is_off(off_stack: TaiStack) -> None:
    # The web channel plugin carries its own store, and without one there is nowhere to
    # register a visitor session — so every door refuses up front with the code the page
    # recognises and stops reconnecting on. The asset door is the one exception: static
    # bundle bytes need no store.
    #
    # The visitor-facing PAGE door is reached by navigating to it, so it refuses with an
    # HTML page rather than the API doors' JSON envelope; the same machine-readable code
    # rides a meta tag. It is asserted first, and separately, for that reason.
    page = await _raw(off_stack, "GET", _WEB_PAGE_PATH)
    assert page.status_code == 501, page.text
    assert page.headers["content-type"].startswith("text/html"), page.headers["content-type"]
    assert _WEB_REFUSAL_CODE_META in page.text, page.text[:800]
    # And it is the refusal page, not the chat shell: serving the shell would boot a chat
    # that can never open a session, so neither the identity-stamped root the bundle reads
    # nor any link to the bundle may appear. A door that regressed to a 200 shell fails on
    # the status above; one that kept the status and served the shell fails here.
    assert "data-identity=" not in page.text, page.text[:800]
    assert _WEB_ASSET_URL_PREFIX not in page.text, page.text[:800]
    # A refused door hands back no session either: the page is the only minter, and it
    # cannot mint one without the registration store.
    assert _WEB_SESSION_COOKIE not in page.cookies

    # The API doors the page's script calls answer the JSON envelope carrying the code.
    for method, path in (
        ("POST", "/api/channels/web/messages"),
        ("GET", "/api/channels/web/stream?identity=e2e-off-site"),
        ("POST", "/api/channels/web/questions/ghost/answer"),
        ("POST", "/api/channels/web/session/rotate"),
    ):
        resp = await _raw(off_stack, method, path, json={} if method == "POST" else None)
        assert resp.status_code == 501, f"{method} {path} -> {resp.status_code}; body: {resp.text}"
        assert resp.headers["content-type"].startswith("application/json"), resp.headers["content-type"]
        assert resp.json().get("code") == _WEB_STORE_OFF_CODE, resp.text
        assert _WEB_SESSION_COOKIE not in resp.cookies


# ---- marketplace registry proxies stay store-independent -----------------


async def test_registry_proxies_are_not_store_gated(off_stack: TaiStack) -> None:
    # search / detail / categories / kinds proxy the registry CLIENT, which is
    # independent of the install STORE. Store-less they still proxy: against an
    # unreachable registry they map to a 502 (bad upstream) — never the store's
    # marketplace-not-configured 501. A 502 is the proof they are not store-gated.
    for method, path in (
        ("GET", "/api/marketplace/search"),
        ("GET", "/api/marketplace/plugins/acme/thing"),
        ("GET", "/api/marketplace/categories"),
        ("GET", "/api/marketplace/kinds"),
    ):
        resp = await _raw(off_stack, method, path)
        assert resp.status_code == 502, f"{method} {path} -> {resp.status_code}; body: {resp.text}"
        # Definitely not refused as a store-OFF feature.
        assert resp.json().get("code") != _MARKETPLACE_CODE, resp.text


# ---- readiness / health / kinds -----------------------------------------


async def test_ready_200_empty_checks_and_health_ok(off_stack: TaiStack) -> None:
    ready = await _raw(off_stack, "GET", "/ready")
    assert ready.status_code == 200, ready.text
    body = ready.json()
    assert body["status"] == "ready"
    # Nothing is gated in, so no store is pinged: the checks map is empty.
    assert body["checks"] == {}

    health = await _raw(off_stack, "GET", "/health")
    assert health.status_code == 200, health.text


async def test_system_kinds_carry_off_rows_for_every_gated_feature(off_stack: TaiStack) -> None:
    rows = await _off_api(off_stack).get("/api/system/kinds")
    state_by_kind = {row["kind"]: row["state"] for row in rows}

    missing = _GATED_OFF_KINDS - state_by_kind.keys()
    assert not missing, f"gated features absent from kinds: {sorted(missing)}"

    not_off = sorted(k for k in _GATED_OFF_KINDS if state_by_kind[k] != "off")
    assert not not_off, f"gated features not reported off: {not_off}"

    # Every row carries the introspection shape (kind/state/plugin/detail).
    for row in rows:
        assert set(row) >= {"kind", "state", "plugin", "detail"}, row
        assert row["state"] in {"active", "default", "off"}, row


# ---- boot log: the OFF state named exactly once --------------------------


async def test_boot_log_names_the_off_state(off_stack: TaiStack) -> None:
    log = off_stack.process("serve").log_path.read_text(encoding="utf-8", errors="replace")

    # Rate limiting is a gated feature like any other: no Redis -> OFF (pass-through)
    # with exactly ONE loud boot WARNING, never a boot refusal.
    assert log.count("rate limiting: OFF") == 1, (
        f"expected exactly one rate-limit WARNING; got {log.count('rate limiting: OFF')}"
    )

    # The skip INFO lines name what is off and are present at boot.
    assert "skipping versioned-preset rehydration" in log
    assert "advisory poll skipped — the skeleton database is not configured" in log
