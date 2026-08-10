"""The auth path is reload-gate-aware.

When identity-provider resolution finds an EMPTY registry (a reload's
reset->reimport window) WHILE the reload gate is held, an authenticated request
gets the SAME retriable ``reloading`` envelope the run surface gives — never a
spurious 401. Outside a reload a missing provider stays a loud fail-closed 401,
and a genuinely invalid key (a resolved provider that rejects it) stays a 401 in
every case, reload or not.
"""

from __future__ import annotations

import json

import pytest
from fastmcp.server.auth import AccessToken
from starlette.authentication import AuthenticationError
from starlette.requests import Request

from tai42_skeleton.access_control.adapter import AuthAdapter, handle_auth_error
from tai42_skeleton.access_control.backend import (
    AccessControlAuthBackend,
    IdentityProviderUnavailableError,
    ReloadInProgressError,
)
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.app.reload_gate import REJECT_MESSAGE, reload_gate


class _ProviderMissingVerifier:
    """Every verify raises the registry-miss class — the reset->reimport window in
    which the identity registry momentarily holds no provider factory."""

    async def verify_token(self, token: str) -> AccessToken | None:
        raise IdentityProviderUnavailableError("identity provider 'redis' is not registered")


class _RejectingVerifier:
    """A resolved provider that authenticates nobody — a genuinely invalid key."""

    async def verify_token(self, token: str) -> AccessToken | None:
        return None


def _conn(headers: dict[str, str], path: str = "/x", method: str = "GET") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": method, "path": path, "query_string": b"", "headers": raw})


def _backend(verifier, settings: AccessControlSettings | None = None) -> AccessControlAuthBackend:
    return AccessControlAuthBackend(verifier, settings or AccessControlSettings())


async def test_provider_missing_during_reload_returns_retriable_reloading():
    # Gate held + empty registry: the authenticated request is answered with the
    # SAME reloading envelope dispatch uses (503, ``reloading: true``, Retry-After),
    # not a 401 — so the client's retry_on_reloading path polls past the window.
    backend = _backend(_ProviderMissingVerifier())
    conn = _conn({"Authorization": "Bearer sk-whatever"})

    async with reload_gate.lock:
        with pytest.raises(ReloadInProgressError) as excinfo:
            await backend.authenticate(conn)

    exc = excinfo.value
    # It routes through AuthenticationMiddleware's on_error (it IS an AuthenticationError).
    assert isinstance(exc, AuthenticationError)

    response = handle_auth_error(conn, exc)
    expected = reload_gate.reject_response()
    # Byte-identical to the shared retriable envelope — the mirror, not a lookalike.
    assert response.status_code == 503
    assert response.status_code != 401
    assert response.body == expected.body
    assert response.headers["Retry-After"] == "5"
    body = json.loads(bytes(response.body))
    assert body["reloading"] is True
    assert body["error"] == REJECT_MESSAGE


async def test_provider_missing_outside_reload_stays_loud_401(caplog):
    # No reload in flight: a missing provider is a real fault. It fails closed as a
    # generic 401 (exactly as before) and is logged loudly server-side.
    backend = _backend(_ProviderMissingVerifier())
    conn = _conn({"Authorization": "Bearer sk-x"})

    assert reload_gate.locked is False
    with caplog.at_level("ERROR"), pytest.raises(AuthenticationError) as excinfo:
        await backend.authenticate(conn)

    assert not isinstance(excinfo.value, ReloadInProgressError)
    assert str(excinfo.value) == "Invalid API key"
    assert "identity provider unavailable" in caplog.text

    response = handle_auth_error(conn, excinfo.value)
    assert response.status_code == 401


async def test_invalid_key_during_reload_still_401():
    # The classes stay distinct: a RESOLVED provider that rejects the key is not the
    # reload transient, so a bad key is a 401 even while the gate is held.
    backend = _backend(_RejectingVerifier())
    conn = _conn({"Authorization": "Bearer sk-bad"})

    async with reload_gate.lock:
        with pytest.raises(AuthenticationError) as excinfo:
            await backend.authenticate(conn)

    assert not isinstance(excinfo.value, ReloadInProgressError)
    assert str(excinfo.value) == "Invalid API key"

    response = handle_auth_error(conn, excinfo.value)
    assert response.status_code == 401


async def test_real_adapter_empty_registry_during_reload_is_retriable():
    # End-to-end through the REAL AuthAdapter: clearing the registry models the
    # reset->reimport window, the adapter's factory wraps the registry-miss KeyError
    # into IdentityProviderUnavailableError, the verifier propagates it, and the
    # backend converts it to the retriable answer while the gate is held. The autouse
    # registry-isolation fixture restores the "redis" registration afterwards.
    from tai42_contract.access_control import registry

    registry._REGISTRY.clear()
    settings = AccessControlSettings()  # auth_providers=["redis"]
    adapter = AuthAdapter(settings)
    backend = AccessControlAuthBackend(adapter._internal_verifier, settings)
    conn = _conn({"X-Api-Key": "sk-x"})

    async with reload_gate.lock:
        with pytest.raises(ReloadInProgressError):
            await backend.authenticate(conn)


async def test_real_adapter_empty_registry_outside_reload_denies_401():
    # The mirror of the integration path: no reload, empty registry -> loud 401 deny.
    from tai42_contract.access_control import registry

    registry._REGISTRY.clear()
    settings = AccessControlSettings()
    adapter = AuthAdapter(settings)
    backend = AccessControlAuthBackend(adapter._internal_verifier, settings)
    conn = _conn({"X-Api-Key": "sk-x"})

    assert reload_gate.locked is False
    with pytest.raises(AuthenticationError) as excinfo:
        await backend.authenticate(conn)
    assert not isinstance(excinfo.value, ReloadInProgressError)
    assert str(excinfo.value) == "Invalid API key"
