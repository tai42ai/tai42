"""Harness self-test for the declared-public interactions doors.

Runs the REAL ``AccessControlVerifier`` against a route registry recording the
interactions doors, with an EMPTY path-pattern table, so an auth-flip on a
declared-public door (media/callback) is caught here WITHOUT booting a stack: the
verifier's declared-public tier publics the ``authed=False`` doors straight from their
registration, while the ``authed=True`` stream stays gated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_skeleton.access_control import verifier as verifier_module
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.access_control.verifier import AccessControlVerifier
from tai42_skeleton.app.route_registry import CORE_OWNER, RouteAction, RouteRegistry

pytestmark = pytest.mark.backendless

# A well-formed served-media reference: the fixed route prefix + a 43-char urlsafe id.
_MEDIA_PATH = "/api/interactions/media/" + "a" * 43
_CALLBACK_PATH = "/api/interactions/callback/some-ticket"
_STREAM_PATH = "/api/interactions/stream"


async def _handler(request: Request) -> Response:
    """A plain handler."""
    return JSONResponse({"data": {}})


class _EmptyStore:
    """The policy store with no route rows and no dynamic patterns: an authed door
    resolves to nothing (gated) without reaching a backend."""

    async def fetch_route(self, path: str) -> str | None:
        return None

    async def fetch_dynamic_patterns(self) -> dict[str, str]:
        return {}


@asynccontextmanager
async def _null_redis(client_cls: Any, settings: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
    """Yields a policy-version reader returning ``None`` (version 0); no other redis op runs."""

    class _R:
        async def get(self, key: str) -> None:
            return None

    yield _R()


def _record(
    registry: RouteRegistry, path: str, methods: list[str], *, public: bool, action: RouteAction | None = None
) -> None:
    registry.record(
        path=path,
        methods=methods,
        name=None,
        handler=_handler,
        summary="s",
        tags=["t"],
        authed=not public,
        action=action,
        request_model=None,
        response_model=None,
        owner=CORE_OWNER,
        public=public,
    )


def _registry() -> RouteRegistry:
    registry = RouteRegistry()
    _record(registry, "/api/interactions/media/{media_id}", ["GET"], public=True)
    _record(registry, "/api/interactions/callback/{ticket}", ["GET", "POST"], public=True)
    _record(registry, "/api/interactions/stream", ["GET"], public=False, action="read")
    return registry


def _verifier(monkeypatch: pytest.MonkeyPatch) -> AccessControlVerifier:
    settings = AccessControlSettings(path_patterns={})
    assert settings.compiled_patterns == []
    monkeypatch.setattr(verifier_module, "route_registry", _registry())
    monkeypatch.setattr(verifier_module, "access_control_store", _EmptyStore)
    monkeypatch.setattr(verifier_module, "client_ctx", _null_redis)
    # The isolated registry surfaces no non-/api GET routes, so the reserved-set memo is empty.
    monkeypatch.setattr(verifier_module, "registered_reserved_get_paths_cached", frozenset)
    return AccessControlVerifier(settings, providers=[])


async def test_media_door_resolves_public(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    # A browser <img> loads the served-media door with no credential, so it must resolve
    # public from its authed=False registration alone.
    assert await v.resolve_resource_ids(_MEDIA_PATH, "GET") == [v.settings.public_resource_id]


async def test_callback_door_resolves_public(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    assert await v.resolve_resource_ids(_CALLBACK_PATH, "GET") == [v.settings.public_resource_id]
    assert await v.resolve_resource_ids(_CALLBACK_PATH, "POST") == [v.settings.public_resource_id]


async def test_stream_stays_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    # The authed studio surface is not declared public: it resolves to nothing (gated),
    # never the public marker. An auth-flip to public would fail this here.
    assert v.settings.public_resource_id not in await v.resolve_resource_ids(_STREAM_PATH, "GET")
