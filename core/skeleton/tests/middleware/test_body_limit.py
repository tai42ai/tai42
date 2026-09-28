"""The app-level ``BodyLimitMiddleware``: it caps every request body at
``TAI_BODY_LIMIT_MAX_BODY_BYTES`` ACTUAL bytes, answers 413 before the inner app
when the body (chunked or declared) is over cap, passes an under-cap body through
byte-identical, ignores non-http scopes, re-raises an over-cap that arrives after
the response has started, and — the load-bearing regression — its escape is NOT a
``ValueError`` subclass, so a route wrapping ``request.json()`` in
``except ValueError`` (the backup import door) answers 413, not 400."""

from __future__ import annotations

import json

import pytest
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

from tai42_skeleton.middleware import body_limit
from tai42_skeleton.middleware.body_limit import BodyLimitMiddleware, _BodyTooLargeError
from tai42_skeleton.settings.body_limit import BodyLimitSettings


def _patch_cap(monkeypatch, cap: int) -> None:
    monkeypatch.setattr(body_limit, "body_limit_settings", lambda: BodyLimitSettings(max_body_bytes=cap))


def _scope(headers: list[tuple[bytes, bytes]] | None = None, scope_type: str = "http") -> Scope:
    return {
        "type": scope_type,
        "method": "POST",
        "path": "/x",
        "headers": headers or [],
        "query_string": b"",
    }


async def _run(app, scope: Scope, chunks: list[tuple[bytes, bool]]) -> list[dict]:
    """Drive ``app`` with the given body chunks (``(bytes, more_body)``); collect
    every ASGI message it sends back."""
    sent: list[dict] = []
    queue = list(chunks)

    async def receive():
        if queue:
            body, more = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": more}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(dict(message))

    await app(scope, receive, send)
    return sent


def _status(sent: list[dict]) -> int | None:
    for message in sent:
        if message["type"] == "http.response.start":
            return message["status"]
    return None


class _Recorder:
    """Inner ASGI app that drains the whole body then answers 200, recording the
    bytes it actually saw and whether it completed."""

    def __init__(self) -> None:
        self.body = b""
        self.completed = False
        self.started = False
        self.called = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.called = True
        while True:
            message = await receive()
            if message["type"] == "http.request":
                self.body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break
        self.completed = True
        self.started = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def test_chunked_over_cap_413_before_inner_app(monkeypatch):
    _patch_cap(monkeypatch, 10)
    inner = _Recorder()
    mw = BodyLimitMiddleware(inner)
    # 20 bytes over a 10-byte cap, chunked (no Content-Length).
    sent = await _run(mw, _scope(), [(b"x" * 8, True), (b"y" * 12, False)])
    assert _status(sent) == 413
    # The inner app never finished reading the body — the over-cap fired first.
    assert inner.completed is False
    assert inner.started is False


async def test_over_cap_content_length_immediate_413(monkeypatch):
    _patch_cap(monkeypatch, 10)
    inner = _Recorder()
    mw = BodyLimitMiddleware(inner)
    sent = await _run(mw, _scope(headers=[(b"content-length", b"999")]), [(b"x" * 999, False)])
    assert _status(sent) == 413
    # Rejected up front on the declared length — the inner app is never invoked.
    assert inner.called is False


async def test_under_cap_passes_through_byte_identical(monkeypatch):
    _patch_cap(monkeypatch, 100)
    inner = _Recorder()
    mw = BodyLimitMiddleware(inner)
    payload = b"hello world"  # 11 bytes, under the cap
    sent = await _run(mw, _scope(headers=[(b"content-length", b"11")]), [(payload, False)])
    assert _status(sent) == 200
    assert inner.completed is True
    # The inner app saw exactly the bytes sent — nothing truncated or altered.
    assert inner.body == payload


async def test_non_http_scope_passes_through(monkeypatch):
    _patch_cap(monkeypatch, 1)
    seen: dict[str, object] = {}

    async def inner(scope, receive, send):
        seen["scope"] = scope

    mw = BodyLimitMiddleware(inner)
    scope = _scope(scope_type="lifespan")

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pass

    await mw(scope, receive, send)
    # A non-http scope is forwarded untouched — no cap read, no wrapping.
    assert seen["scope"] is scope


async def test_over_cap_after_response_start_reraises(monkeypatch):
    _patch_cap(monkeypatch, 10)

    async def inner(scope, receive, send):
        # Start the response BEFORE reading the (over-cap) body.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            message = await receive()  # eventually raises _BodyTooLargeError
            if message["type"] == "http.request" and not message.get("more_body", False):
                break

    mw = BodyLimitMiddleware(inner)
    # The stream is already committed, so the over-cap cannot become a 413 — it
    # surfaces loudly instead of a silently truncated response.
    with pytest.raises(_BodyTooLargeError):
        await _run(mw, _scope(), [(b"x" * 20, False)])


# -- route-level regression: the escape is not a ValueError ------------------


async def test_over_cap_to_json_route_returns_413_not_400(monkeypatch):
    # The backup import door wraps ``request.json()`` in ``except ValueError``. The
    # over-cap escape must NOT be a ValueError subclass, or it would be swallowed
    # into a 400; it is a 413 instead. Driven over a bare ASGI wrapper (no
    # Starlette error middleware) so the escape reaches BodyLimitMiddleware cleanly.
    from tai42_skeleton.routers.backup import import_backup

    _patch_cap(monkeypatch, 10)

    async def bare_route(scope, receive, send):
        request = Request(scope, receive)
        response = await import_backup(request)
        await response(scope, receive, send)

    mw = BodyLimitMiddleware(bare_route)
    body = json.dumps({"document": {"version": 1, "sections": {}}, "sections": []}).encode()
    assert len(body) > 10  # over the cap, chunked (no Content-Length)
    sent = await _run(mw, _scope(), [(body, False)])
    assert _status(sent) == 413


# -- per-route body bound ----------------------------------------------------


def _scope_for(path: str, content_length: bytes | None = None) -> Scope:
    headers = [(b"content-length", content_length)] if content_length is not None else []
    return {"type": "http", "method": "POST", "path": path, "headers": headers, "query_string": b""}


def _uploads_surface() -> list:
    from tai42_skeleton.app.route_registry import RouteMetadata

    return [
        RouteMetadata(
            path="/api/channels/web/uploads",
            methods=("POST",),
            name="uploads",
            summary="s",
            description="",
            tags=("t",),
            authed=False,
            request_model=None,
            response_model=None,
            reload_gated=False,
            reads_body=True,
            error_statuses=(),
            success_status=200,
            additional_success_statuses=(),
            success_media_types={},
            action="write",
            max_body_bytes=30,
        ),
    ]


async def test_per_route_bound_honoured_for_declared_route(monkeypatch):
    # A route declaring max_body_bytes accepts a body over the global cap and rejects one over its
    # OWN bound; a route declaring none still enforces the global cap. Both guards honour the bound.
    _patch_cap(monkeypatch, 10)  # the app-wide default
    monkeypatch.setattr(body_limit, "load_all_routes", _uploads_surface)
    body_limit._reset_body_limit_door_table_cache()
    try:
        # 20 bytes: over the 10-byte global, under the route's 30-byte bound → accepted.
        inner = _Recorder()
        sent = await _run(BodyLimitMiddleware(inner), _scope_for("/api/channels/web/uploads"), [(b"x" * 20, False)])
        assert _status(sent) == 200
        assert inner.completed is True

        # 40 bytes: over the route's 30-byte bound → 413 (the running-total guard honours the bound).
        sent = await _run(
            BodyLimitMiddleware(_Recorder()), _scope_for("/api/channels/web/uploads"), [(b"y" * 40, False)]
        )
        assert _status(sent) == 413

        # A route declaring none still rejects a body over the 10-byte global.
        sent = await _run(BodyLimitMiddleware(_Recorder()), _scope_for("/other"), [(b"z" * 20, False)])
        assert _status(sent) == 413

        # The up-front Content-Length reject honours the per-route bound: 40 > 30 → immediate 413.
        rejected = _Recorder()
        sent = await _run(BodyLimitMiddleware(rejected), _scope_for("/api/channels/web/uploads", b"40"), [])
        assert _status(sent) == 413
        assert rejected.called is False

        # ... and a Content-Length within the per-route bound but over the global passes up front.
        inner2 = _Recorder()
        sent = await _run(
            BodyLimitMiddleware(inner2), _scope_for("/api/channels/web/uploads", b"20"), [(b"x" * 20, False)]
        )
        assert _status(sent) == 200
        assert inner2.completed is True
    finally:
        body_limit._reset_body_limit_door_table_cache()


async def test_per_route_bound_matched_under_a_prefix_stripping_proxy(monkeypatch):
    # A proxy that strips the mount prefix leaves ``scope["path"]`` unprefixed while
    # ``root_path`` names the mount. The lookup runs on the root_path-stripped path
    # (unchanged here — it never carried the prefix), so the route's declared 30-byte bound
    # is still found: a 20-byte body over the 10-byte global cap but under it is accepted.
    _patch_cap(monkeypatch, 10)
    monkeypatch.setattr(body_limit, "load_all_routes", _uploads_surface)
    body_limit._reset_body_limit_door_table_cache()
    try:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/channels/web/uploads",
            "root_path": "/prefix",
            "headers": [],
            "query_string": b"",
        }
        inner = _Recorder()
        sent = await _run(BodyLimitMiddleware(inner), scope, [(b"x" * 20, False)])
        assert _status(sent) == 200
        assert inner.completed is True
    finally:
        body_limit._reset_body_limit_door_table_cache()


async def test_per_route_bound_matched_when_prefix_carried_in_path(monkeypatch):
    # A deployment that keeps the mount prefix IN ``scope["path"]`` (a first-class
    # ``Mount("/x", app)`` or a proxy forwarding the prefix under ``--root-path /x``). The
    # lookup strips ``root_path`` first — the same canonical path the router matches — so
    # ``/x/api/channels/web/uploads`` resolves to the same door and its 30-byte bound
    # applies: a 20-byte body over the 10-byte global cap is accepted, a 40-byte one is 413.
    _patch_cap(monkeypatch, 10)
    monkeypatch.setattr(body_limit, "load_all_routes", _uploads_surface)
    body_limit._reset_body_limit_door_table_cache()
    try:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/x/api/channels/web/uploads",
            "root_path": "/x",
            "headers": [],
            "query_string": b"",
        }
        inner = _Recorder()
        sent = await _run(BodyLimitMiddleware(inner), scope, [(b"x" * 20, False)])
        assert _status(sent) == 200
        assert inner.completed is True

        # 40 bytes: over the route's own 30-byte bound → 413 (the declared bound was found).
        sent = await _run(BodyLimitMiddleware(_Recorder()), dict(scope), [(b"y" * 40, False)])
        assert _status(sent) == 413
    finally:
        body_limit._reset_body_limit_door_table_cache()


async def test_per_route_bound_not_applied_under_a_different_prefix(monkeypatch):
    # ``strip_root_path`` strips ONLY when the path starts with ``root_path``: a request
    # under ``/y`` while ``root_path`` is ``/x`` is left intact, so it resolves to no
    # declared door and the app-wide 10-byte cap applies — a 20-byte body is 413.
    _patch_cap(monkeypatch, 10)
    monkeypatch.setattr(body_limit, "load_all_routes", _uploads_surface)
    body_limit._reset_body_limit_door_table_cache()
    try:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/y/api/channels/web/uploads",
            "root_path": "/x",
            "headers": [],
            "query_string": b"",
        }
        sent = await _run(BodyLimitMiddleware(_Recorder()), scope, [(b"x" * 20, False)])
        assert _status(sent) == 413
    finally:
        body_limit._reset_body_limit_door_table_cache()


async def test_over_cap_413_inside_server_error_middleware(monkeypatch):
    # Production layering: BodyLimitMiddleware runs INSIDE the base app's own
    # Starlette ``ServerErrorMiddleware`` (TaiMCP._base_middleware passes it into the
    # base-app middleware list). Were the cap placed OUTSIDE the error handler, that
    # handler would catch the raised ``_BodyTooLargeError`` and commit a 500 before the
    # cap could answer 413. Driving an over-cap body through a real Starlette app —
    # which wraps its middleware list inside ServerErrorMiddleware exactly as the
    # base app does — must still yield 413.
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    _patch_cap(monkeypatch, 10)

    async def echo(request: Request) -> JSONResponse:
        await request.json()  # reads the over-cap body -> _BodyTooLargeError
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[Route("/x", echo, methods=["POST"])],
        middleware=[Middleware(BodyLimitMiddleware)],
    )
    sent = await _run(app, _scope(), [(b"x" * 20, False)])
    assert _status(sent) == 413
