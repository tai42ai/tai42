"""``tai42_app.accounts.authenticated_credential``: the credential the access-control gate verified.

A neutral consumer route reads the facet behind a gate stand-in that runs the real
backend's candidate selection (``Authorization`` first, then ``X-Api-Key``; the first
candidate that verifies wins) and places the authenticated principal in the scope as the
backend does.
"""

from __future__ import annotations

import httpx
import pytest
from fastmcp.server.auth import AccessToken
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send
from tai42_contract.app import tai42_app

from tai42_skeleton.access_control.backend import AccessControlAuthBackend
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.access_control.user import TaiUser
from tai42_skeleton.app.instance import app as tai_app

_LIVE = "cred-live"
_STALE = "cred-stale"


class _Verifier:
    """Verifies exactly the live credential."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != _LIVE:
            return None
        return AccessToken(token=token, client_id="principal-1", scopes=[], claims={})


class _Gate:
    """Places the verified principal in ``scope["user"]`` the way the auth backend does."""

    def __init__(self, inner: ASGIApp) -> None:
        self._inner = inner
        self._backend = AccessControlAuthBackend(_Verifier(), AccessControlSettings())  # type: ignore[arg-type]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        access_token = await self._backend._get_access_token(Request(scope))
        if access_token is not None:
            scope["user"] = TaiUser(access_token)
        await self._inner(scope, receive, send)


async def _consumer(request: Request) -> JSONResponse:
    return JSONResponse({"credential": tai42_app.accounts.authenticated_credential(request)})


@pytest.fixture
def client():
    consumer = Starlette(routes=[Route("/consumer", _consumer)])
    with tai42_app.bound(tai_app):
        yield httpx.AsyncClient(transport=httpx.ASGITransport(app=_Gate(consumer)), base_url="http://test")


async def _credential(client: httpx.AsyncClient, headers: dict[str, str]) -> str | None:
    response = await client.get("/consumer", headers=headers)
    assert response.status_code == 200
    return response.json()["credential"]


async def test_the_verified_bearer_credential_is_returned(client: httpx.AsyncClient) -> None:
    assert await _credential(client, {"Authorization": f"Bearer {_LIVE}"}) == _LIVE


async def test_a_stale_first_candidate_yields_the_verified_second(client: httpx.AsyncClient) -> None:
    headers = {"Authorization": f"Bearer {_STALE}", "X-Api-Key": _LIVE}
    assert await _credential(client, headers) == _LIVE


async def test_an_unauthenticated_request_has_no_credential(client: httpx.AsyncClient) -> None:
    assert await _credential(client, {}) is None
