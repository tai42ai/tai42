"""Test seams for tai42-accounts-oidc.

``FakeRedis`` stands in for the state/SSO/session records (no real Redis). A real
loopback OIDC issuer (``oidc_server``) serves discovery/JWKS and a configurable
token/userinfo endpoint minting real RSA-signed id_tokens, so the login flow's
httpx exchange and id_token verification run end to end with no external I/O.
Registry + settings/holder/runtimes isolation keeps env-dependent tests from
leaking. The ``tai42_app`` handle is bound to a no-op fake at import so the route
decorators register cleanly.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import RSAKey
from starlette.requests import Request

# -- bind a no-op app handle before any route module imports --------------------


class _FakeHttp:
    def custom_route(self, *args: Any, **kwargs: Any):
        def _decorator(func):
            return func

        return _decorator


class _FakeLifecycle:
    def __init__(self) -> None:
        self.startup_handlers: list[Any] = []

    def on_startup(self, func):
        self.startup_handlers.append(func)
        return func


class _FakeAccounts:
    """The ``tai42_app.accounts`` facet: resolves the current epoch's recorded provider
    instance by name (``make_provider`` records ``accounts-oidc`` here), so the routes'
    ``active_provider`` resolution finds the provider under test."""

    def __init__(self) -> None:
        self.providers: dict[str, Any] = {}

    def active_provider(self, name: str) -> Any:
        return self.providers.get(name)


class _FakeApp:
    def __init__(self) -> None:
        self.http = _FakeHttp()
        self.lifecycle = _FakeLifecycle()
        self.accounts = _FakeAccounts()


_fake_app = _FakeApp()

from tai42_contract.app import tai42_app  # noqa: E402

tai42_app.bind(_fake_app)


# -- FakeRedis ------------------------------------------------------------------


class FakeRedis:
    """The string/TTL operations the plugin's records use.

    ``ex``/``pexpire`` are recorded so a test can assert the TTL posture without a
    real clock. ``getdel`` is the single-use read the state/SSO records depend on.
    """

    def __init__(self, *, raise_on: set[str] | None = None) -> None:
        self.store: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.pexpire_calls: list[tuple[str, int]] = []
        self._raise_on = raise_on or set()

    def _maybe_raise(self, op: str) -> None:
        if op in self._raise_on:
            raise RuntimeError(f"injected redis failure on {op}")

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._maybe_raise("set")
        self.store[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        self._maybe_raise("get")
        return self.store.get(key)

    async def getdel(self, key: str) -> str | None:
        self._maybe_raise("getdel")
        self.expirations.pop(key, None)
        return self.store.pop(key, None)

    async def delete(self, *keys: str) -> int:
        self._maybe_raise("delete")
        removed = 0
        for key in keys:
            self.expirations.pop(key, None)
            if self.store.pop(key, None) is not None:
                removed += 1
        return removed

    async def pexpire(self, key: str, ms: int) -> bool:
        self._maybe_raise("pexpire")
        self.pexpire_calls.append((key, ms))
        if key not in self.store:
            return False
        self.expirations[key] = ms // 1000
        return True


def make_client_ctx(fake: FakeRedis):
    """A drop-in for ``client_ctx`` yielding ``fake`` for any client class."""

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake

    return _ctx


# -- a real loopback OIDC issuer ------------------------------------------------


class _IssuerHandler(http.server.BaseHTTPRequestHandler):
    """Serve per-path GET/POST responses configured by the test."""

    def _handle(self) -> None:
        routes: dict[str, dict[str, Any]] = self.server.routes  # type: ignore[attr-defined]
        self.server.requests.append((self.command, self.path))  # type: ignore[attr-defined]
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
        route = routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body: bytes = route["body"]
        self.send_response(route.get("status", 200))
        self.send_header("Content-Type", route.get("content_type", "application/json"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = _handle
    do_POST = _handle
    do_HEAD = _handle

    def log_message(self, *args: Any) -> None:  # silence per-request logging
        pass


class IssuerServer:
    """A loopback OIDC issuer whose discovery, JWKS, token, and userinfo responses
    each test configures per path. Mints real RSA-signed id_tokens."""

    def __init__(self, base_url: str, server: http.server.HTTPServer, key: RSAKey) -> None:
        self.base_url = base_url
        self._server = server
        self.key = key

    @property
    def requests(self) -> list[tuple[str, str]]:
        return self._server.requests  # type: ignore[attr-defined]

    def set_route(
        self, path: str, *, body: bytes = b"", status: int = 200, content_type: str = "application/json"
    ) -> None:
        self._server.routes[path] = {"body": body, "status": status, "content_type": content_type}  # type: ignore[attr-defined]

    def set_json(self, path: str, obj: Any, *, status: int = 200) -> None:
        self.set_route(path, body=json.dumps(obj).encode(), status=status)

    def configure_oidc(self, *, client_id: str = "client-1") -> None:
        """Wire discovery + JWKS so the flow can discover and verify."""
        self.set_json(
            "/.well-known/openid-configuration",
            {
                "issuer": self.base_url,
                "jwks_uri": f"{self.base_url}/jwks",
                "authorization_endpoint": f"{self.base_url}/authorize",
                "token_endpoint": f"{self.base_url}/token",
                "userinfo_endpoint": f"{self.base_url}/userinfo",
            },
        )
        self.set_json("/jwks", {"keys": [self.key.as_dict(private=False)]})

    def id_token(self, *, sub: str = "alice", aud: str = "client-1", nonce: str | None = None, **overrides: Any) -> str:
        claims: dict[str, Any] = {
            "iss": self.base_url,
            "aud": aud,
            "sub": sub,
            "exp": int(time.time()) + 3600,
        }
        if nonce is not None:
            claims["nonce"] = nonce
        claims.update(overrides)
        return joserfc_jwt.encode({"alg": "RS256", "kid": self.key.kid}, claims, self.key)


def _serve_issuer(key: RSAKey) -> Iterator[IssuerServer]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _IssuerHandler)
    port = server.server_address[1]
    server.routes = {}  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield IssuerServer(f"http://127.0.0.1:{port}", server, key)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def signing_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": "test-key-1"})


@pytest.fixture
def oidc_server(signing_key: RSAKey) -> Iterator[IssuerServer]:
    """A real loopback OIDC issuer serving test-configured discovery/JWKS/token."""
    yield from _serve_issuer(signing_key)


# -- request building -----------------------------------------------------------


def build_request(
    body: Any = None,
    *,
    method: str = "GET",
    query_string: bytes = b"",
    path_params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> Request:
    """A Starlette ``Request`` with an optional JSON body, query string, and path
    params — enough to drive a route handler directly."""
    payload = json.dumps(body).encode() if body is not None else b""
    header_items = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "headers": header_items,
        "client": ("198.51.100.7", 40000),
        "path": "/",
        "query_string": query_string,
        "path_params": path_params or {},
    }

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, _receive)


def response_json(response: Any) -> Any:
    return json.loads(bytes(response.body))


# -- provider construction ------------------------------------------------------


@pytest.fixture
def make_provider(monkeypatch: pytest.MonkeyPatch):
    """Build an ``OidcAccountsProvider`` from a providers list, wiring the env, the
    settings cache, and the FakeRedis ``client_ctx`` seam. Returns ``(provider,
    fake_redis)``. Pass ``state_key``/``public_base_url`` as ``None`` to exercise
    the required-setting guards."""
    from types import SimpleNamespace

    from tai42_accounts_oidc import provider as provider_mod
    from tai42_accounts_oidc.settings import accounts_oidc_settings

    def _make(
        providers: list[dict[str, Any]],
        *,
        state_key: str | None = "hmac-signing-secret",
        public_base_url: str | None = "https://studio.example.com",
        redis: FakeRedis | None = None,
    ) -> tuple[Any, FakeRedis]:
        monkeypatch.setenv("TAI_ACCOUNTS_OIDC_PROVIDERS", json.dumps(providers))
        if state_key is None:
            monkeypatch.delenv("TAI_ACCOUNTS_OIDC_STATE_KEY", raising=False)
        else:
            monkeypatch.setenv("TAI_ACCOUNTS_OIDC_STATE_KEY", state_key)
        if public_base_url is None:
            monkeypatch.delenv("TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL", raising=False)
        else:
            monkeypatch.setenv("TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL", public_base_url)
        accounts_oidc_settings.cache_clear()

        fake = redis if redis is not None else FakeRedis()
        monkeypatch.setattr(provider_mod, "client_ctx", make_client_ctx(fake))
        settings_obj: Any = SimpleNamespace(redis=SimpleNamespace(), admin=None)
        instance = provider_mod.OidcAccountsProvider(settings_obj)
        # Record the instance as the active provider so the routes' module helpers
        # (``provider_settings`` / ``get_runtime``) resolve it through the accounts facet.
        _fake_app.accounts.providers["accounts-oidc"] = instance
        return instance, fake

    return _make


# -- isolation fixtures ---------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_registries() -> Iterator[None]:
    """Snapshot + restore both module-level registries around each test so a
    registration never leaks."""
    from tai42_contract.access_control import registry as identity_registry
    from tai42_contract.accounts import registry as accounts_registry

    identity_saved = dict(identity_registry._REGISTRY)
    accounts_saved = dict(accounts_registry._REGISTRY)
    try:
        yield
    finally:
        identity_registry._REGISTRY.clear()
        identity_registry._REGISTRY.update(identity_saved)
        accounts_registry._REGISTRY.clear()
        accounts_registry._REGISTRY.update(accounts_saved)


@pytest.fixture(autouse=True)
def _reset_plugin_state() -> Iterator[None]:
    """Reset the settings cache and the recorded active provider so an env-dependent
    test starts clean and never leaks into the next (provider state lives on the
    per-epoch instance now, recorded via the fake accounts facet — no module holder)."""
    from tai42_accounts_oidc.settings import accounts_oidc_settings

    accounts_oidc_settings.cache_clear()
    _fake_app.accounts.providers.clear()
    yield
    accounts_oidc_settings.cache_clear()
    _fake_app.accounts.providers.clear()
