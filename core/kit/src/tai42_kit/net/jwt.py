"""OIDC discovery, JWKS fetching, and JWT verification.

This module is behind the ``tai42-kit[jwt]`` optional extra: it imports ``joserfc``
at module top (the kit driver-extra pattern), so it is deliberately NOT
re-exported from :mod:`tai42_kit.net` — a plain ``import tai42_kit.net`` must keep
working for every install that did not opt into ``[jwt]``. Consumers import from
here directly: ``from tai42_kit.net.jwt import verify_jwt``.

Issuer discovery and JWKS URLs are OPERATOR CONFIG, not caller-supplied input, so
these fetches do NOT run through the SSRF guard that :mod:`tai42_kit.net.fetch_url`
applies: the guard blocks private address ranges, which would hard-break every
self-hosted internal IdP (a Keycloak on a LAN IP is a first-class deployment).
The fetches are instead hardened explicitly — connect/read timeouts, a streamed
response size cap, redirects disabled (a redirecting endpoint is a loud typed
error), and ``https`` required except for loopback hosts (dev/e2e issuers).

Every failure path raises a typed error; nothing returns a ``None`` that hides a
reason, and verification fails closed.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import re
import time
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from joserfc import jwt as _jwt
from joserfc.errors import JoseError
from joserfc.jwk import Key, KeySet, KeySetSerialization


class JwtError(Exception):
    """Base class for every error this module raises."""


class JwksFetchError(JwtError):
    """A discovery or JWKS fetch failed: transport error, non-200 status, a
    redirect, the size cap, an ``https`` violation, a malformed document, or an
    issuer that does not match the discovery document's own ``issuer``."""


class JwtVerifyError(JwtError):
    """A token failed verification: bad structure, an algorithm outside the
    allowlist, an unknown ``kid`` after a refetch, a bad signature, or an
    ``iss``/``aud``/``exp``/``nbf``/``nonce`` claim failure. The message names
    which check refused the token."""


_JWT_STRUCTURE = re.compile(r"\A[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")


def looks_like_jwt(token: str) -> bool:
    """Return whether ``token`` is structurally a compact JWS: three
    dot-separated non-empty base64url segments.

    This is a pure string shape check — it decodes nothing beyond confirming the
    base64url charset. Consumers use it as a cheap chain gate so non-JWT
    credentials (API keys, session tokens) fall through without a verification
    attempt.
    """
    return _JWT_STRUCTURE.match(token) is not None


@dataclass(frozen=True)
class OidcDiscovery:
    """The subset of an OIDC discovery document this ecosystem consumes."""

    issuer: str
    jwks_uri: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str | None


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_fetchable_scheme(url: str) -> None:
    """Reject a URL that is not ``https`` (loopback ``http`` excepted)."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and _is_loopback_host(parts.hostname):
        return
    raise JwksFetchError(f"Refusing to fetch {url!r}: https is required except for loopback hosts.")


async def _fetch_json(url: str, *, timeout: float, max_bytes: int) -> dict[str, Any]:
    """GET ``url`` and return its parsed JSON object under the module's hardening:
    ``https`` (or loopback ``http``), no redirects, a streamed size cap, and a
    connect/read timeout. Every failure mode is a :class:`JwksFetchError`.
    """
    _require_fetchable_scheme(url)
    try:
        async with (
            httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(timeout)) as client,
            client.stream("GET", url) as response,
        ):
            if response.is_redirect:
                location = response.headers.get("Location")
                raise JwksFetchError(f"Refusing redirect from {url!r} to {location!r}: redirects are not followed.")
            if response.status_code != 200:
                raise JwksFetchError(f"Fetching {url!r} returned status {response.status_code}.")
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > max_bytes:
                    raise JwksFetchError(f"Response from {url!r} exceeded the {max_bytes}-byte size cap.")
    except httpx.HTTPError as exc:
        raise JwksFetchError(f"Transport error fetching {url!r}: {exc}") from exc

    try:
        document = json.loads(bytes(buffer))
    except json.JSONDecodeError as exc:
        raise JwksFetchError(f"Response from {url!r} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise JwksFetchError(f"Response from {url!r} is not a JSON object.")
    return document


async def fetch_discovery(issuer: str, *, timeout: float = 5.0, max_bytes: int = 65536) -> OidcDiscovery:
    """Fetch and validate ``{issuer}/.well-known/openid-configuration``.

    Enforces the module's transport hardening and the OIDC issuer-match rule: the
    document's own ``issuer`` MUST equal the configured ``issuer`` exactly, else
    the discovery is rejected (a mismatch is a misconfigured or hostile issuer).
    """
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    document = await _fetch_json(url, timeout=timeout, max_bytes=max_bytes)

    document_issuer = document.get("issuer")
    if document_issuer != issuer:
        raise JwksFetchError(
            f"Discovery at {url!r} declares issuer {document_issuer!r}, which does not match the "
            f"configured issuer {issuer!r}."
        )

    return OidcDiscovery(
        issuer=issuer,
        jwks_uri=_require_str(document, "jwks_uri", url),
        authorization_endpoint=_require_str(document, "authorization_endpoint", url),
        token_endpoint=_require_str(document, "token_endpoint", url),
        userinfo_endpoint=_optional_str(document, "userinfo_endpoint", url),
    )


def _require_str(document: dict[str, Any], field: str, url: str) -> str:
    value = document.get(field)
    if not isinstance(value, str):
        raise JwksFetchError(f"Discovery at {url!r} is missing a string {field!r} field.")
    return value


def _optional_str(document: dict[str, Any], field: str, url: str) -> str | None:
    value = document.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise JwksFetchError(f"Discovery at {url!r} has a non-string {field!r} field.")
    return value


class JwksCache:
    """A per-issuer JWKS cache with TTL refresh and bounded unknown-``kid`` refetch.

    Keys are served from an in-memory map. The map is refreshed when it is empty
    or older than ``ttl_seconds``. An unknown ``kid`` — the signal of an issuer
    key rotation — triggers at most one forced refetch, and only if at least
    ``refetch_cooldown_seconds`` have elapsed since the last forced refetch; this
    supports rotation without letting attacker-chosen random ``kid`` values turn
    the verifier into a JWKS-hammering client. Concurrent callers collapse behind
    a single in-flight fetch via an asyncio lock.
    """

    def __init__(
        self,
        jwks_uri: str,
        *,
        ttl_seconds: float = 3600.0,
        refetch_cooldown_seconds: float = 60.0,
        timeout: float = 5.0,
        max_bytes: int = 65536,
    ) -> None:
        self._jwks_uri = jwks_uri
        self._ttl_seconds = ttl_seconds
        self._refetch_cooldown_seconds = refetch_cooldown_seconds
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._lock = asyncio.Lock()
        self._keys: dict[str, Key] = {}
        # ``monotonic`` clocks drive TTL and cooldown so they are immune to
        # wall-clock adjustments. ``None`` means "no fetch has succeeded yet".
        self._fetched_at: float | None = None
        self._last_forced_refetch: float | None = None

    async def get_key(self, kid: str, alg: str) -> Key:
        """Return the key for ``kid``, refreshing or refetching as the caching
        policy allows. Raises :class:`JwtVerifyError` when the ``kid`` is unknown
        after the allowed refetch (or when a refetch is barred by the cooldown).
        """
        async with self._lock:
            now = time.monotonic()
            if self._fetched_at is None or (now - self._fetched_at) >= self._ttl_seconds:
                await self._refresh()

            key = self._keys.get(kid)
            if key is not None:
                return key

            now = time.monotonic()
            if (
                self._last_forced_refetch is not None
                and (now - self._last_forced_refetch) < self._refetch_cooldown_seconds
            ):
                raise JwtVerifyError(
                    f"Unknown kid {kid!r} and a JWKS refetch is within its {self._refetch_cooldown_seconds}s cooldown."
                )
            self._last_forced_refetch = now
            await self._refresh()

            key = self._keys.get(kid)
            if key is not None:
                return key
            raise JwtVerifyError(f"Unknown kid {kid!r} after a JWKS refetch.")

    async def _refresh(self) -> None:
        """Fetch the JWKS document and rebuild the key map. Callers hold the lock."""
        document = await _fetch_json(self._jwks_uri, timeout=self._timeout, max_bytes=self._max_bytes)
        try:
            key_set = KeySet.import_key_set(cast(KeySetSerialization, document))
        except (JoseError, ValueError, KeyError, TypeError) as exc:
            raise JwksFetchError(f"JWKS at {self._jwks_uri!r} is not a valid key set: {exc}") from exc
        self._keys = {key.kid: key for key in key_set.keys if key.kid}
        self._fetched_at = time.monotonic()


def _decode_header(token: str) -> dict[str, Any]:
    """Decode a compact JWS header segment without verifying anything."""
    segment = token.split(".", 1)[0]
    padding = "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(segment + padding)
        header = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise JwtVerifyError(f"Token header is not valid base64url JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise JwtVerifyError("Token header is not a JSON object.")
    return header


async def verify_jwt(
    token: str,
    *,
    jwks: JwksCache,
    issuer: str,
    audience: str,
    allowed_algs: tuple[str, ...] = ("RS256",),
    leeway_seconds: float = 60.0,
    expected_nonce: str | None = None,
) -> dict[str, Any]:
    """Verify ``token`` against ``jwks`` and return its claims, or raise.

    The order is deliberate and fails closed at each step: structural check;
    header parse; ``alg`` must be in ``allowed_algs`` BEFORE any key lookup (so
    ``alg=none`` and symmetric downgrades are rejected by construction); ``kid``
    required; key fetch via the cache; signature verification; then claims —
    ``iss`` exact, ``aud`` contains ``audience``, ``exp`` required, ``exp``/``nbf``
    with leeway. When ``expected_nonce`` is given, the ``nonce`` claim must equal
    it exactly (the OIDC id_token replay binding).
    """
    if not looks_like_jwt(token):
        raise JwtVerifyError("Token is not a structurally valid JWT.")

    header = _decode_header(token)
    alg = header.get("alg")
    if alg not in allowed_algs:
        raise JwtVerifyError(f"Token alg {alg!r} is not in the allowed set {allowed_algs!r}.")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise JwtVerifyError("Token header is missing a 'kid'.")

    key = await jwks.get_key(kid, alg)

    try:
        decoded = _jwt.decode(token, key, algorithms=list(allowed_algs))
    except JoseError as exc:
        raise JwtVerifyError(f"Token signature verification failed: {exc}") from exc

    registry = _jwt.JWTClaimsRegistry(
        leeway=int(leeway_seconds),
        iss={"essential": True, "value": issuer},
        aud={"essential": True, "value": audience},
        exp={"essential": True},
    )
    try:
        registry.validate(decoded.claims)
    except JoseError as exc:
        raise JwtVerifyError(f"Token claims verification failed: {exc}") from exc

    if expected_nonce is not None and decoded.claims.get("nonce") != expected_nonce:
        raise JwtVerifyError("Token 'nonce' claim is missing or does not match the expected value.")

    return decoded.claims
