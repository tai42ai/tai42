"""Fixtures for the validate-only OIDC identity provider.

A real localhost OIDC issuer on an ephemeral port serves a test-configured
``/.well-known/openid-configuration`` + ``/jwks`` pair; RSA keypairs are generated
in-test via joserfc and real signed JWTs are minted from them, so the whole suite
runs with zero real network I/O. Plus registry isolation (the plugin's
``identity-oidc`` registration is snapshotted and restored around each test) and
``TAI_IDENTITY_OIDC_*`` env hygiene so a test's injected config is the only input.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import RSAKey


def make_rsa_key(kid: str) -> RSAKey:
    """Generate an in-test RSA signing key carrying ``kid``."""
    return RSAKey.generate_key(2048, parameters={"kid": kid})


def sign(key: RSAKey, claims: dict[str, Any], *, kid: str, alg: str = "RS256") -> str:
    """Sign ``claims`` into a compact JWS with ``key`` under header ``kid``/``alg``.

    ``kid`` is set independently of the signing key so a test can name a published
    kid on a token signed by a different key (the bad-signature case).
    """
    return joserfc_jwt.encode({"alg": alg, "kid": kid}, claims, key)


class _OidcHandler(http.server.BaseHTTPRequestHandler):
    """Serve per-path OIDC responses (discovery, JWKS) configured by the test."""

    def do_GET(self) -> None:  # http.server dispatch name
        routes: dict[str, dict[str, Any]] = self.server.routes  # type: ignore[attr-defined]
        self.server.requests.append(self.path)  # type: ignore[attr-defined]
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
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence per-request logging
        pass


class OidcServer:
    """A loopback OIDC issuer: discovery + JWKS + in-test token signing.

    A default signing key is generated and published at construction, and the
    discovery document declares this server's own base URL as its ``issuer`` (the
    OIDC issuer-match rule). Tests can rotate the published key set, break routes,
    and sign tokens with any generated key.
    """

    def __init__(self, base_url: str, server: http.server.HTTPServer) -> None:
        self.base_url = base_url
        self._server = server
        self._keys: dict[str, RSAKey] = {}
        self.default_kid = "sig-key-1"
        self._keys[self.default_kid] = make_rsa_key(self.default_kid)
        self.publish(self.default_kid)
        self.set_discovery()

    @property
    def requests(self) -> list[str]:
        return self._server.requests  # type: ignore[attr-defined]

    def set_route(
        self,
        path: str,
        *,
        body: bytes = b"",
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self._server.routes[path] = {  # type: ignore[attr-defined]
            "body": body,
            "status": status,
            "content_type": content_type,
        }

    def set_json(self, path: str, obj: Any) -> None:
        self.set_route(path, body=json.dumps(obj).encode())

    def set_discovery(self, *, issuer: str | None = None) -> None:
        """(Re)serve the discovery document, declaring ``issuer`` (default: this
        server's own base URL) as its issuer."""
        self.set_json(
            "/.well-known/openid-configuration",
            {
                "issuer": issuer if issuer is not None else self.base_url,
                "jwks_uri": f"{self.base_url}/jwks",
                "authorization_endpoint": f"{self.base_url}/authorize",
                "token_endpoint": f"{self.base_url}/token",
            },
        )

    def add_key(self, kid: str) -> RSAKey:
        """Generate and retain a new key (NOT yet published to the JWKS)."""
        key = make_rsa_key(kid)
        self._keys[kid] = key
        return key

    def publish(self, *kids: str) -> None:
        """Serve exactly ``kids`` (public halves only) at the JWKS endpoint."""
        jwks = {"keys": [self._keys[kid].as_dict(private=False) for kid in kids]}
        self.set_json("/jwks", jwks)

    def sign(
        self,
        claims: dict[str, Any],
        *,
        kid: str | None = None,
        signing_kid: str | None = None,
        alg: str = "RS256",
    ) -> str:
        """Sign ``claims`` and stamp header ``kid`` (default: the primary key).

        ``signing_kid`` selects the key that actually signs (default: the header
        ``kid``). A different ``signing_kid`` forges the bad-signature case.
        """
        header_kid = kid or self.default_kid
        signer = self._keys[signing_kid or header_kid]
        return sign(signer, claims, kid=header_kid, alg=alg)


def _serve_oidc() -> Iterator[OidcServer]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _OidcHandler)
    port = server.server_address[1]
    server.routes = {}  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield OidcServer(f"http://127.0.0.1:{port}", server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def oidc_server() -> Iterator[OidcServer]:
    """A real loopback OIDC issuer serving test-configured discovery + JWKS."""
    yield from _serve_oidc()


@pytest.fixture(autouse=True)
def _clear_oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the plugin's env namespace so a test's injected config is the only
    input (an ambient ``TAI_IDENTITY_OIDC_*`` var must never leak into a test)."""
    for name in (
        "TAI_IDENTITY_OIDC_ISSUER",
        "TAI_IDENTITY_OIDC_AUDIENCE",
        "TAI_IDENTITY_OIDC_ALLOWED_ALGS",
        "TAI_IDENTITY_OIDC_CLAIM",
        "TAI_IDENTITY_OIDC_JWKS_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_identity_registry() -> Iterator[None]:
    """Snapshot + restore the module-level identity-provider registry around each
    test so a registration never leaks into the next."""
    from tai42_contract.access_control import registry

    saved = dict(registry._REGISTRY)
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved)


class AcSettingsStub:
    """A stand-in for the skeleton's AC settings object the factory receives.

    The provider accepts it to satisfy the factory contract but reads nothing from
    it (its config is the ``TAI_IDENTITY_OIDC_*`` env namespace).
    """

    key_prefix: str = ""
    redis: Any = None
