"""A deterministic in-process OAuth2 authorization server, thread-hosted in the
pytest process. Serves ``/authorize`` and ``/token`` so a connector can run the
full OAuth connect + refresh flow against a stub instead of a live vendor."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.ports import allocate_port


@dataclass
class _OAuthState:
    refresh_count: int = 0
    expires_in: int = 3600
    issued_access_tokens: list[str] = field(default_factory=list)
    # When set, ``/authorize`` redirects back with an OAuth error instead of a
    # code — the scripted authorization-denied path the browser-e2e negative drives.
    deny: bool = False


class OAuthIdp:
    """A deterministic in-memory OAuth2 authorization server for the connector suite.

    ``/authorize`` redirects back to the connector callback with a code, ``/token``
    grants and refreshes access tokens, and refresh grants are counted for the
    refresh-lock test. Access tokens are opaque; the lifetime ``/token`` reports is
    controllable so a test can force the next-issued token already expired. The
    authorization decision is scriptable (:meth:`set_deny`, or the ``/_deny`` /
    ``/_allow`` control endpoints over HTTP) so a test can drive the refused-consent
    negative in which ``/authorize`` redirects back with ``error=access_denied``."""

    def __init__(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.host = host
        # A caller may pin the port (the browser-e2e runner does, so a spec can reach
        # the connector's authorization server at a known origin); otherwise take an
        # ephemeral one.
        self.port = port if port is not None else allocate_port()
        self._state = _OAuthState()
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def refresh_count(self) -> int:
        return self._state.refresh_count

    @property
    def issued_access_tokens(self) -> list[str]:
        """Every access token this server has minted — the encryption-at-rest test
        asserts none of them appears verbatim in the stored connection blob."""
        return list(self._state.issued_access_tokens)

    def set_expires_in(self, seconds: int) -> None:
        """Set the lifetime of freshly issued access tokens (a value ``<= 0``
        makes the next-issued token already expired, forcing a refresh)."""
        self._state.expires_in = seconds

    def set_deny(self, deny: bool) -> None:
        """Script the authorization decision: when ``deny`` is true the next
        ``/authorize`` redirects back with ``error=access_denied`` (no code)
        instead of granting one — the authorization-refused negative."""
        self._state.deny = deny

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/authorize")
        async def authorize(redirect_uri: str, state: str = "") -> RedirectResponse:
            sep = "&" if "?" in redirect_uri else "?"
            if self._state.deny:
                # The authorization is refused: return the OAuth error redirect the
                # callback page relays back to the app, never a code.
                return RedirectResponse(url=f"{redirect_uri}{sep}error=access_denied&state={state}", status_code=302)
            code = f"code-{uuid.uuid4().hex[:8]}"
            return RedirectResponse(url=f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)

        @app.post("/token")
        async def token(request: Request) -> JSONResponse:
            grant = await _form_or_json(request)
            if grant.get("grant_type") == "refresh_token":
                self._state.refresh_count += 1
            access = f"at-{uuid.uuid4().hex}"
            refresh = f"rt-{uuid.uuid4().hex}"
            self._state.issued_access_tokens.append(access)
            return JSONResponse(
                {
                    "access_token": access,
                    "refresh_token": refresh,
                    "token_type": "Bearer",
                    "expires_in": self._state.expires_in,
                    "scope": "read",
                }
            )

        # Out-of-process control: a Playwright spec in a separate Node process flips the
        # denial knob over HTTP (the in-process ``set_deny`` cannot cross the boundary).
        @app.post("/_deny")
        async def deny_authorize() -> JSONResponse:
            self.set_deny(True)
            return JSONResponse({"deny": True})

        @app.post("/_allow")
        async def allow_authorize() -> JSONResponse:
            self.set_deny(False)
            return JSONResponse({"deny": False})

        return app


async def _form_or_json(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    form = await request.form()
    return dict(form.items())
