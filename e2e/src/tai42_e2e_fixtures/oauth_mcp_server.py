"""An MCP server that requires OAuth and is its own authorization server, for the e2e connector suite.

One loopback process serves a protected MCP endpoint (``/mcp``, tools ``ping`` and ``echo``), its
protected-resource metadata (RFC 9728), its authorization-server metadata (RFC 8414), client
registration (RFC 7591), ``/authorize`` (PKCE S256, ``resource``, ``iss`` on the redirect),
``/token`` (code and refresh grants, client authentication by the client's registered method, the
refresh token rotated on every refresh) and ``/revoke``. It speaks the newest MCP authorization
revision, so it can also accept a client-identity document: a URL ``client_id`` whose document it
fetches and checks.

Thread-hosted in the pytest process like the other harness servers. Its behaviour is switched by
:class:`Controls` (in process with :meth:`OAuthMcpServer.set`, or over HTTP at ``/_control/*`` for a
driver in another process) and everything it saw is counted (:meth:`OAuthMcpServer.counters`). It is a
protocol fixture: no vendor's shape, and no secret handling beyond what the assertions need.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tai42_e2e.ports import allocate_port
from tai42_e2e.waiting import wait_for, wait_for_async

_MCP_PATH = "/mcp"
_PRM_PATH = "/.well-known/oauth-protected-resource/mcp"
_AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
_ISSUER_PATH = "/tenant"
_DEFAULT_SCOPE = "mcp"
_WRONG_ISSUER = "https://wrong-issuer.e2e.test"
# The platform's refresh deadline: the longest it waits for an answer to a refresh grant.
_REFRESH_HOLD_DEADLINE = 20.0


@dataclass
class Controls:
    """The fixture's switches; every one off (the plain, well-behaved server) by default."""

    # Turn client registration off (no ``registration_endpoint``; ``/register`` answers 404).
    registration_off: bool = False
    # Issue no refresh token.
    no_refresh_token: bool = False
    # Leave ``expires_in`` out of token responses.
    omit_expires_in: bool = False
    # Answer ``initialize`` without a token but refuse ``tools/list`` (and everything else).
    unchallenged_initialize: bool = False
    # Answer ``initialize`` and ``tools/list`` without a token; refuse ``tools/call``.
    anonymous_listing: bool = False
    # Publish the resource as the server's origin rather than the MCP endpoint.
    resource_as_origin: bool = False
    # Publish an issuer with a path (``<base>/tenant``).
    issuer_with_path: bool = False
    # A token endpoint the metadata names instead of the fixture's own (a private address).
    token_endpoint: str | None = None
    # Hold the answer to every refresh grant until :meth:`OAuthMcpServer.release_refreshes`.
    hold_refreshes: bool = False
    # Leave ``code_challenge_methods_supported`` out of the metadata.
    omit_pkce_methods: bool = False
    # Send a wrong ``iss`` on the authorization redirect.
    wrong_iss: bool = False
    # Send no ``iss`` on the redirect while the metadata advertises it.
    omit_iss: bool = False
    # Stop advertising ``authorization_response_iss_parameter_supported`` but still send ``iss``.
    unadvertised_iss: bool = False
    # Accept client-identity documents (advertise ``client_id_metadata_document_supported``).
    client_id_document: bool = False
    # Leave ``offline_access`` out of the metadata's ``scopes_supported``.
    no_offline_access: bool = False
    # ``{tool name: scope}``: a call to the tool is answered 403 with an ``insufficient_scope``
    # challenge naming the scope until a token carrying it is presented.
    scope_demands: dict[str, str] = field(default_factory=dict)


@dataclass
class _Client:
    client_id: str
    secret: str | None
    method: str
    redirect_uris: list[str]
    document: bool = False


@dataclass
class _Code:
    client_id: str
    redirect_uri: str
    challenge: str
    resource: str | None
    scopes: list[str]


@dataclass
class _Token:
    client_id: str
    scopes: list[str]
    resource: str | None
    expires_at: float


@dataclass
class Counters:
    """What the fixture saw, for the assertions."""

    # Each registration: its ``redirect_uris``, ``application_type`` and auth method.
    registrations: list[dict[str, Any]] = field(default_factory=list)
    # Each refresh grant the token endpoint answered: the client, the ``resource`` it carried, and
    # whether its refresh token was accepted.
    refreshes: list[dict[str, Any]] = field(default_factory=list)
    # Each revocation: ``kind`` is ``refresh``, ``access`` or ``unknown``.
    revocations: list[dict[str, Any]] = field(default_factory=list)
    # Each ``/authorize`` request's parameters, in order.
    authorizations: list[dict[str, str]] = field(default_factory=list)
    # Each ``/token`` request's form (secrets left out), in order.
    token_requests: list[dict[str, str]] = field(default_factory=list)
    # Each client-identity document fetch: the URL and whether it was accepted.
    document_fetches: list[dict[str, Any]] = field(default_factory=list)
    # ``{tool name: calls that reached the MCP endpoint}``.
    tool_calls: dict[str, int] = field(default_factory=dict)
    # Each MCP request: its JSON-RPC method and every header it carried.
    mcp_requests: list[dict[str, Any]] = field(default_factory=list)


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _ThreadedLifespanServer:
    """A uvicorn server on a thread of the pytest process, running the app's lifespan."""

    def __init__(self, app: ASGIApp, host: str, port: int) -> None:
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        wait_for(
            lambda: self._server.started or not self._thread.is_alive(),
            deadline=15.0,
            message="the OAuth MCP fixture server never started",
        )
        if not self._server.started:
            raise RuntimeError("the OAuth MCP fixture server exited before it started")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10.0)


class OAuthMcpServer:
    """A protected MCP server and its own authorization server on a loopback port."""

    def __init__(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.host = host
        self.port = port if port is not None else allocate_port()
        self._lock = threading.Lock()
        self._controls = Controls()
        self._counters = Counters()
        self._clients: dict[str, _Client] = {}
        self._codes: dict[str, _Code] = {}
        self._access: dict[str, _Token] = {}
        self._refresh: dict[str, _Token] = {}
        # ``{https origin: (address to fetch from, Host header)}`` for client-identity documents.
        self._document_hosts: dict[str, tuple[str, str]] = {}
        self._registered = 0
        self._releases = 0
        self._server = _ThreadedLifespanServer(self._build_app(), host, self.port)

    # -- addresses -------------------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}{_MCP_PATH}"

    @property
    def issuer(self) -> str:
        return f"{self.base_url}{_ISSUER_PATH}" if self._controls.issuer_with_path else self.base_url

    @property
    def resource(self) -> str:
        return self.base_url if self._controls.resource_as_origin else self.mcp_url

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    # -- controls and counters -------------------------------------------------------------------

    def set(self, **changes: Any) -> None:
        """Switch controls by name (see :class:`Controls`)."""
        known = {f.name for f in fields(Controls)}
        unknown = sorted(set(changes) - known)
        if unknown:
            raise ValueError(f"unknown control(s): {unknown}")
        with self._lock:
            for name, value in changes.items():
                setattr(self._controls, name, value)

    def counters(self) -> Counters:
        """A copy of what the fixture saw so far."""
        with self._lock:
            return Counters(**json.loads(json.dumps(asdict(self._counters))))

    def expire_access_tokens(self) -> None:
        """Expire every access token issued so far (the platform's stored expiry stays as it was)."""
        with self._lock:
            for token in self._access.values():
                token.expires_at = 0.0

    def release_refreshes(self) -> None:
        """Answer every refresh grant the ``hold_refreshes`` control is holding now."""
        with self._lock:
            self._releases += 1

    def forget_client(self, client_id: str) -> None:
        """Forget a registered client, so its next authenticated request is answered ``invalid_client``."""
        with self._lock:
            self._clients.pop(client_id, None)

    def register_by_hand(self, client_id: str, *, secret: str | None, method: str, redirect_uris: list[str]) -> None:
        """Register an app by hand, as an administrator does in a sign-in service's console."""
        with self._lock:
            self._clients[client_id] = _Client(client_id, secret, method, list(redirect_uris))

    def serve_documents_of(self, origin: str, *, address: str, host: str) -> None:
        """Fetch the client-identity documents of ``https`` ``origin`` from ``address`` with ``Host: host``.

        The stack under test serves plain http, so its ``https`` origin is reached this way.
        """
        with self._lock:
            self._document_hosts[origin] = (address.rstrip("/"), host)

    def registered_clients(self) -> list[str]:
        with self._lock:
            return [c.client_id for c in self._clients.values() if not c.document]

    # -- the app ---------------------------------------------------------------------------------

    def _build_app(self) -> ASGIApp:
        mcp = FastMCP("e2e-oauth-mcp")

        @mcp.tool
        def ping() -> dict:
            """Return ``{"ok": "pong"}``."""
            return {"ok": "pong"}

        @mcp.tool
        def echo(payload: str) -> dict:
            """Return ``{"echoed": payload}``."""
            return {"echoed": payload}

        mcp_app = mcp.http_app(path=_MCP_PATH, stateless_http=True, json_response=True)
        routes = [
            Route(_PRM_PATH, self._protected_resource, methods=["GET"]),
            Route(_AS_METADATA_PATH, self._metadata, methods=["GET"]),
            Route(f"{_AS_METADATA_PATH}{_ISSUER_PATH}", self._metadata_with_path, methods=["GET"]),
            Route("/register", self._register, methods=["POST"]),
            Route("/authorize", self._authorize, methods=["GET"]),
            Route("/token", self._token, methods=["POST"]),
            Route("/revoke", self._revoke, methods=["POST"]),
            Route("/_control/set", self._control_set, methods=["POST"]),
            Route("/_control/expire-access-tokens", self._control_expire, methods=["POST"]),
            Route("/_control/forget-client", self._control_forget, methods=["POST"]),
            Route("/_control/release-refreshes", self._control_release, methods=["POST"]),
            Route("/_control/register-by-hand", self._control_register, methods=["POST"]),
            Route("/_control/serve-documents", self._control_documents, methods=["POST"]),
            Route("/_control/counters", self._control_counters, methods=["GET"]),
            Mount("/", app=mcp_app),
        ]
        app = Starlette(routes=routes, lifespan=mcp_app.lifespan)
        return _McpGuard(app, self)

    # -- metadata --------------------------------------------------------------------------------

    async def _protected_resource(self, request: Request) -> Response:
        return JSONResponse(
            {
                "resource": self.resource,
                "authorization_servers": [self.issuer],
                "scopes_supported": [_DEFAULT_SCOPE],
                "resource_name": "E2E OAuth MCP",
            }
        )

    def _metadata_document(self) -> dict[str, Any]:
        controls = self._controls
        scopes = [_DEFAULT_SCOPE] if controls.no_offline_access else [_DEFAULT_SCOPE, "offline_access"]
        document: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.base_url}/authorize",
            "token_endpoint": controls.token_endpoint or f"{self.base_url}/token",
            "revocation_endpoint": f"{self.base_url}/revoke",
            "scopes_supported": scopes,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
            "authorization_response_iss_parameter_supported": not controls.unadvertised_iss,
            "client_id_metadata_document_supported": controls.client_id_document,
        }
        if not controls.registration_off:
            document["registration_endpoint"] = f"{self.base_url}/register"
        if not controls.omit_pkce_methods:
            document["code_challenge_methods_supported"] = ["S256"]
        return document

    async def _metadata(self, request: Request) -> Response:
        if self._controls.issuer_with_path:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(self._metadata_document())

    async def _metadata_with_path(self, request: Request) -> Response:
        if not self._controls.issuer_with_path:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(self._metadata_document())

    # -- registration and authorization ----------------------------------------------------------

    async def _register(self, request: Request) -> Response:
        if self._controls.registration_off:
            return JSONResponse({"error": "not_found"}, status_code=404)
        body = await request.json()
        method = body.get("token_endpoint_auth_method") or "client_secret_basic"
        redirect_uris = list(body.get("redirect_uris") or [])
        with self._lock:
            self._registered += 1
            client_id = f"dyn-{self._registered}-{secrets.token_hex(3)}"
            secret = None if method == "none" else secrets.token_urlsafe(16)
            self._clients[client_id] = _Client(client_id, secret, method, redirect_uris)
            self._counters.registrations.append(
                {
                    "client_id": client_id,
                    "redirect_uris": redirect_uris,
                    "application_type": body.get("application_type"),
                    "token_endpoint_auth_method": method,
                }
            )
        answer: dict[str, Any] = {
            "client_id": client_id,
            "token_endpoint_auth_method": method,
            "redirect_uris": redirect_uris,
            "client_id_issued_at": int(time.time()),
        }
        if secret is not None:
            answer["client_secret"] = secret
            answer["client_secret_expires_at"] = 0
        return JSONResponse(answer, status_code=201)

    async def _document_client(self, client_id: str) -> _Client | None:
        split = urlsplit(client_id)
        origin = f"{split.scheme}://{split.netloc}"
        with self._lock:
            target = self._document_hosts.get(origin)
        if target is None:
            return None
        address, host = target
        url = f"{address}{split.path}"
        accepted = False
        document: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                response = await http.get(url, headers={"Host": host})
            if response.status_code == 200:
                document = response.json()
                accepted = document.get("client_id") == client_id
        finally:
            with self._lock:
                self._counters.document_fetches.append({"client_id": client_id, "accepted": accepted})
        if not accepted:
            return None
        return _Client(client_id, None, "none", list(document.get("redirect_uris") or []), document=True)

    async def _authorize(self, request: Request) -> Response:
        params = dict(request.query_params)
        with self._lock:
            self._counters.authorizations.append(params)
            client = self._clients.get(params.get("client_id", ""))
            controls = self._controls
        client_id = params.get("client_id", "")
        if client is None and controls.client_id_document and client_id.startswith("https://"):
            client = await self._document_client(client_id)
            if client is not None:
                with self._lock:
                    self._clients[client_id] = client
        if client is None:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        redirect_uri = params.get("redirect_uri", "")
        if redirect_uri not in client.redirect_uris:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri is not registered"}, status_code=400
            )
        if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
            return JSONResponse(
                {"error": "invalid_request", "error_description": "PKCE S256 required"}, status_code=400
            )
        code = secrets.token_urlsafe(16)
        with self._lock:
            self._codes[code] = _Code(
                client_id=client.client_id,
                redirect_uri=redirect_uri,
                challenge=params["code_challenge"],
                resource=params.get("resource"),
                scopes=params.get("scope", "").split(),
            )
        answer = {"code": code, "state": params.get("state", "")}
        iss = self._redirect_iss()
        if iss is not None:
            answer["iss"] = iss
        separator = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{separator}{urlencode(answer)}", status_code=302)

    def _redirect_iss(self) -> str | None:
        controls = self._controls
        if controls.wrong_iss:
            return _WRONG_ISSUER
        if controls.unadvertised_iss:
            return self.issuer
        if controls.omit_iss:
            return None
        return self.issuer

    # -- the token and revocation endpoints ------------------------------------------------------

    def _authenticate(self, request: Request, form: dict[str, str]) -> _Client | None:
        """The client a token-endpoint request authenticates as by its registered method, else ``None``."""
        header = request.headers.get("authorization", "")
        basic_id = basic_secret = None
        if header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode()
            except ValueError:
                return None
            basic_id, _, basic_secret = decoded.partition(":")
        client_id = basic_id or form.get("client_id", "")
        with self._lock:
            client = self._clients.get(client_id)
        if client is None:
            return None
        if client.method == "client_secret_basic":
            return client if basic_id == client.client_id and basic_secret == client.secret else None
        if client.method == "client_secret_post":
            return client if form.get("client_secret") == client.secret and basic_id is None else None
        return client if basic_id is None and "client_secret" not in form else None

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> dict[str, Any]:
        access = secrets.token_urlsafe(24)
        with self._lock:
            controls = self._controls
            self._access[access] = _Token(client_id, scopes, resource, time.time() + 3600)
            answer: dict[str, Any] = {"access_token": access, "token_type": "Bearer", "scope": " ".join(scopes)}
            if not controls.omit_expires_in:
                answer["expires_in"] = 3600
            if not controls.no_refresh_token:
                refresh = secrets.token_urlsafe(24)
                self._refresh[refresh] = _Token(client_id, scopes, resource, float("inf"))
                answer["refresh_token"] = refresh
        return answer

    async def _token(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        with self._lock:
            self._counters.token_requests.append(
                {k: v for k, v in form.items() if k not in ("client_secret", "code_verifier")}
            )
        client = self._authenticate(request, form)
        if client is None:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        grant = form.get("grant_type")
        if grant == "authorization_code":
            with self._lock:
                code = self._codes.pop(form.get("code", ""), None)
            if code is None or code.client_id != client.client_id or code.redirect_uri != form.get("redirect_uri"):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if _s256(form.get("code_verifier", "")) != code.challenge:
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"}, status_code=400)
            if code.resource is not None and form.get("resource") != code.resource:
                return JSONResponse({"error": "invalid_target"}, status_code=400)
            return JSONResponse(self._issue(client.client_id, code.scopes, code.resource))
        if grant == "refresh_token":
            if self._controls.hold_refreshes:
                released = self._releases

                async def due() -> bool:
                    return self._releases != released

                # The platform abandons a refresh after its refresh deadline (20 s); a hold released
                # later would rotate a refresh token the platform never stored, so a hold the test
                # does not release inside that deadline fails here.
                await wait_for_async(
                    due,
                    deadline=_REFRESH_HOLD_DEADLINE,
                    message="a held refresh was never released inside the platform's refresh deadline",
                )
            with self._lock:
                held = self._refresh.pop(form.get("refresh_token", ""), None)
                self._counters.refreshes.append(
                    {"client_id": client.client_id, "resource": form.get("resource"), "accepted": held is not None}
                )
            if held is None or held.client_id != client.client_id:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if "resource" in form and held.resource is not None and form["resource"] != held.resource:
                return JSONResponse({"error": "invalid_target"}, status_code=400)
            return JSONResponse(self._issue(client.client_id, held.scopes, held.resource))
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    async def _revoke(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        token = form.get("token", "")
        with self._lock:
            if self._refresh.pop(token, None) is not None:
                kind = "refresh"
            elif self._access.pop(token, None) is not None:
                kind = "access"
            else:
                kind = "unknown"
            self._counters.revocations.append({"kind": kind})
        return Response(status_code=200)

    # -- the MCP endpoint's gate -----------------------------------------------------------------

    def _challenge(self, status: int, *, error: str | None = None, scope: str | None = None) -> Response:
        params = [f'resource_metadata="{self.base_url}{_PRM_PATH}"']
        if error is not None:
            params.append(f'error="{error}"')
        if scope is not None:
            params.append(f'scope="{scope}"')
        return JSONResponse(
            {"error": error or "unauthorized"},
            status_code=status,
            headers={"WWW-Authenticate": "Bearer " + ", ".join(params)},
        )

    def gate(self, headers: dict[str, str], rpc_method: str | None, tool: str | None) -> Response | None:
        """The answer that stops an MCP request at the gate, or ``None`` to serve it."""
        with self._lock:
            self._counters.mcp_requests.append({"method": rpc_method, "headers": dict(headers)})
            controls = self._controls
            authorization = headers.get("authorization", "")
            token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
            held = self._access.get(token) if token else None
            if tool is not None:
                self._counters.tool_calls[tool] = self._counters.tool_calls.get(tool, 0) + 1
        if not token:
            anonymous = ("initialize", "notifications/initialized")
            if controls.anonymous_listing and rpc_method in (*anonymous, "tools/list"):
                return None
            if controls.unchallenged_initialize and rpc_method in anonymous:
                return None
            return self._challenge(401)
        if held is None or held.expires_at <= time.time():
            return self._challenge(401, error="invalid_token")
        needed = controls.scope_demands.get(tool or "")
        if needed is not None and needed not in held.scopes:
            return self._challenge(403, error="insufficient_scope", scope=needed)
        return None

    # -- HTTP controls ---------------------------------------------------------------------------

    async def _control_set(self, request: Request) -> Response:
        self.set(**(await request.json()))
        return JSONResponse({"ok": True})

    async def _control_expire(self, request: Request) -> Response:
        self.expire_access_tokens()
        return JSONResponse({"ok": True})

    async def _control_release(self, request: Request) -> Response:
        self.release_refreshes()
        return JSONResponse({"ok": True})

    async def _control_forget(self, request: Request) -> Response:
        self.forget_client((await request.json())["client_id"])
        return JSONResponse({"ok": True})

    async def _control_register(self, request: Request) -> Response:
        body = await request.json()
        self.register_by_hand(
            body["client_id"], secret=body.get("secret"), method=body["method"], redirect_uris=body["redirect_uris"]
        )
        return JSONResponse({"ok": True})

    async def _control_documents(self, request: Request) -> Response:
        body = await request.json()
        self.serve_documents_of(body["origin"], address=body["address"], host=body["host"])
        return JSONResponse({"ok": True})

    async def _control_counters(self, request: Request) -> Response:
        return JSONResponse(asdict(self.counters()))


class _McpGuard:
    """The ASGI gate in front of the MCP endpoint: reads the JSON-RPC body, asks the server, replays it."""

    def __init__(self, app: ASGIApp, server: OAuthMcpServer) -> None:
        self._app = app
        self._server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != _MCP_PATH:
            await self._app(scope, receive, send)
            return
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        rpc_method, tool = _rpc(body)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        refused = self._server.gate(headers, rpc_method, tool)
        if refused is not None:
            await refused(scope, receive, send)
            return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self._app(scope, replay, send)


def _rpc(body: bytes) -> tuple[str | None, str | None]:
    """The JSON-RPC method of a single-message body, and the tool a ``tools/call`` names."""
    if not body:
        return None, None
    try:
        message = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(message, dict):
        return None, None
    method = message.get("method")
    params = message.get("params")
    tool = params.get("name") if method == "tools/call" and isinstance(params, dict) else None
    return method, tool
