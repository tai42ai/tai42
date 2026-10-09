"""Discovery of the authorization server behind an MCP server (MCP authorization; RFC 9728, RFC 8414).

:func:`discover` is the only reader of server metadata. For an MCP URL it probes the server once
without a credential (a ``WWW-Authenticate`` challenge may name the protected-resource document and
the scopes to ask for), reads the protected-resource document, checks its ``resource`` is the MCP URL
or a parent of it, picks one listed authorization server, and reads that server's metadata. Every
request runs through the kit URL guard (``https`` unless loopback, no redirects, a size cap, one total
deadline): the MCP URL is typed by a user and every further URL is named by a remote server.

The two documents are cached in process for :data:`CACHE_TTL_SECONDS` (the protected-resource document
per MCP URL, the server metadata per issuer); the choice of server and every check run on each call,
so a caller bound to one issuer never sees another caller's choice. Only a call that succeeds stores
what it fetched.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, ValidationError, field_validator
from tai42_contract.errors import ErrorKind
from tai42_kit.net import (
    GuardedRequestError,
    GuardedResponse,
    UrlGuardError,
    guarded_request,
    is_loopback_host,
)

from tai42_skeleton.connectors.oauth.client import OAUTH_HTTP_TIMEOUT_SECONDS, OAuthError

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600.0
DISCOVERY_MAX_BYTES = 64 * 1024

DiscoveryStep = Literal[
    "challenge",
    "resource-metadata",
    "resource-mismatch",
    "authorization-server-metadata",
    "issuer-mismatch",
    "pkce-unsupported",
    "url-refused",
]


class OAuthDiscoveryError(OAuthError):
    """Discovery failed at ``step``; nothing carrying a secret or a token was sent."""

    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR

    def __init__(self, message: str, *, step: DiscoveryStep) -> None:
        """Build the error for the discovery ``step`` that failed."""
        super().__init__(f"OAuth discovery failed at {step}: {message}")
        self.step: DiscoveryStep = step


# -- the WWW-Authenticate parser and the issuer comparison -------------------------------------


@dataclass(frozen=True)
class BearerChallenge:
    """The parameters of a ``Bearer`` challenge the MCP authorization flow reads."""

    resource_metadata: str | None
    scope: tuple[str, ...] | None
    error: str | None


# The RFC 9110 ``token`` character class (an HTTP grammar term, not a credential).
_HTTP_TOKEN_CHARS = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"  # noqa: S105
_PARAM_RE = re.compile(rf"\s*({_HTTP_TOKEN_CHARS})\s*=\s*")
_SCHEME_RE = re.compile(rf"\s*({_HTTP_TOKEN_CHARS})(?=\s|,|$)")


def _read_value(value: str, pos: int) -> tuple[str, int]:
    if pos < len(value) and value[pos] == '"':
        out: list[str] = []
        pos += 1
        while pos < len(value):
            ch = value[pos]
            if ch == "\\" and pos + 1 < len(value):
                out.append(value[pos + 1])
                pos += 2
                continue
            if ch == '"':
                return "".join(out), pos + 1
            out.append(ch)
            pos += 1
        return "".join(out), pos
    end = pos
    while end < len(value) and value[end] not in ", \t":
        end += 1
    return value[pos:end], end


def _parse_challenges(value: str) -> list[tuple[str, dict[str, str]]]:
    challenges: list[tuple[str, dict[str, str]]] = []
    pos = 0
    current: dict[str, str] | None = None
    while pos < len(value):
        while pos < len(value) and value[pos] in ", \t":
            pos += 1
        if pos >= len(value):
            break
        param = _PARAM_RE.match(value, pos)
        if param is not None and current is not None:
            text, pos = _read_value(value, param.end())
            current[param.group(1).lower()] = text
            continue
        scheme = _SCHEME_RE.match(value, pos)
        if scheme is None:
            # A token68 or a malformed fragment: skip to the next separator.
            while pos < len(value) and value[pos] not in ", \t":
                pos += 1
            continue
        current = {}
        challenges.append((scheme.group(1).lower(), current))
        pos = scheme.end()
    return challenges


def parse_www_authenticate(value: str | None) -> BearerChallenge | None:
    """Read the ``Bearer`` challenge of a ``WWW-Authenticate`` value, or ``None`` when it carries none.

    Reads ``resource_metadata``, ``scope`` (a space-separated list) and ``error``, quoted or bare. Any
    shape it cannot read yields no candidate, never an error.
    """
    if not value:
        return None
    for scheme, params in _parse_challenges(value):
        if scheme != "bearer":
            continue
        scope = params.get("scope")
        return BearerChallenge(
            resource_metadata=params.get("resource_metadata") or None,
            scope=tuple(part for part in scope.split(" ") if part) if scope is not None else None,
            error=params.get("error") or None,
        )
    return None


def same_issuer(a: str | None, b: str | None) -> bool:
    """The engine's one issuer comparison: plain string equality, with no normalisation of any kind.

    Case, port, a trailing slash and percent-encoding all count; two ``None`` (no issuer) are equal.
    """
    return a == b


# -- documents -----------------------------------------------------------------------------------


def _check_absolute(value: str) -> str:
    split = urlsplit(value)
    if split.scheme not in ("http", "https") or not split.netloc:
        raise ValueError(f"not an absolute http(s) URL: {value!r}")
    return value


class _ProtectedResourceDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resource: StrictStr
    authorization_servers: list[StrictStr]
    scopes_supported: list[StrictStr] | None = None
    resource_name: StrictStr | None = None

    @field_validator("resource")
    @classmethod
    def _check_resource(cls, value: str) -> str:
        return _check_absolute(value)

    @field_validator("authorization_servers")
    @classmethod
    def _check_servers(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("authorization_servers is empty")
        return [_check_absolute(item) for item in value]


class _AuthorizationServerMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    issuer: StrictStr
    authorization_endpoint: StrictStr | None = None
    token_endpoint: StrictStr | None = None
    registration_endpoint: StrictStr | None = None
    revocation_endpoint: StrictStr | None = None
    code_challenge_methods_supported: list[StrictStr] | None = None
    scopes_supported: list[StrictStr] | None = None
    token_endpoint_auth_methods_supported: list[StrictStr] | None = None
    client_id_metadata_document_supported: StrictBool = False
    authorization_response_iss_parameter_supported: StrictBool = False


@dataclass(frozen=True)
class _ResourceEntry:
    document: _ProtectedResourceDocument
    challenge_scopes: tuple[str, ...] | None


@dataclass(frozen=True)
class DiscoveredAuthorization:
    """What discovery found for one MCP URL and the authorization server chosen for the caller.

    Every URL and the ``resource`` are the strings exactly as published.
    """

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    revocation_endpoint: str | None
    resource: str
    resource_name: str | None
    # The scopes the server's 401 challenge named (``None``: it named none).
    challenge_scopes: tuple[str, ...] | None
    # The protected-resource document's ``scopes_supported`` (empty when it lists none).
    scopes_supported: tuple[str, ...]
    offline_access_supported: bool
    token_endpoint_auth_methods_supported: tuple[str, ...] | None
    client_id_document_supported: bool
    iss_parameter_supported: bool
    # Every authorization server the protected-resource document lists, in its order.
    authorization_servers: tuple[str, ...]


# -- cache -------------------------------------------------------------------------------------

_resource_cache: dict[str, tuple[float, _ResourceEntry]] = {}
_metadata_cache: dict[str, tuple[float, _AuthorizationServerMetadata]] = {}


def _monotonic() -> float:
    return time.monotonic()


def _cached[T](cache: dict[str, tuple[float, T]], key: str, now: float) -> T | None:
    entry = cache.get(key)
    if entry is None or entry[0] <= now:
        return None
    return entry[1]


def forget(mcp_url: str) -> None:
    """Drop, in the calling process, the cached document of ``mcp_url`` and the metadata of every issuer it lists."""
    entry = _resource_cache.pop(mcp_url, None)
    if entry is None:
        return
    for issuer in entry[1].document.authorization_servers:
        _metadata_cache.pop(issuer, None)


def reset_discovery_cache() -> None:
    """Drop every cached document (tests)."""
    _resource_cache.clear()
    _metadata_cache.clear()


# -- requests ----------------------------------------------------------------------------------


async def _request(method: str, url: str, **kwargs: Any) -> GuardedResponse:
    try:
        return await guarded_request(
            method, url, timeout=OAUTH_HTTP_TIMEOUT_SECONDS, max_bytes=DISCOVERY_MAX_BYTES, **kwargs
        )
    except UrlGuardError as exc:
        raise OAuthDiscoveryError(str(exc), step="url-refused") from exc


def _json_object(response: GuardedResponse) -> dict[str, Any] | None:
    if response.status != 200:
        return None
    try:
        payload = json.loads(response.body)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


async def _close_probe_session(mcp_url: str, session_id: str) -> None:
    try:
        await _request("DELETE", mcp_url, headers={"Mcp-Session-Id": session_id})
    except Exception:
        # Best effort: the probe's session times out on the server either way.
        logger.debug("connectors: closing the discovery probe's session at %s failed", mcp_url, exc_info=True)


async def _challenge(mcp_url: str) -> BearerChallenge | None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "tai42", "version": "1"},
        },
    }
    try:
        response = await _request(
            "POST", mcp_url, json_body=body, headers={"Accept": "application/json, text/event-stream"}
        )
    except GuardedRequestError as exc:
        raise OAuthDiscoveryError(str(exc), step="challenge") from exc
    if response.status == 401:
        return parse_www_authenticate(response.headers.get("www-authenticate"))
    session_id = response.headers.get("mcp-session-id")
    if 200 <= response.status < 300 and session_id:
        await _close_probe_session(mcp_url, session_id)
    return None


def _strip_one_slash(path: str) -> str:
    return path.removesuffix("/")


def _resource_matches(published: str, mcp_url: str) -> bool:
    """``published`` equals ``mcp_url`` or is a parent of it, by path segment, one trailing slash ignored."""
    doc = urlsplit(published)
    mcp = urlsplit(mcp_url)
    if doc.scheme.lower() != mcp.scheme.lower() or doc.netloc.lower() != mcp.netloc.lower():
        return False
    doc_path = _strip_one_slash(doc.path)
    mcp_path = _strip_one_slash(mcp.path)
    return doc_path == "" or mcp_path == doc_path or mcp_path.startswith(doc_path + "/")


async def _fetch_resource(mcp_url: str) -> _ResourceEntry:
    challenge = await _challenge(mcp_url)
    split = urlsplit(mcp_url)
    origin = f"{split.scheme}://{split.netloc}"
    candidates: list[str] = []
    if challenge is not None and challenge.resource_metadata:
        candidates.append(challenge.resource_metadata)
    if split.path not in ("", "/"):
        candidates.append(f"{origin}/.well-known/oauth-protected-resource{split.path}")
    candidates.append(f"{origin}/.well-known/oauth-protected-resource")
    outcomes: list[str] = []
    mismatch = False
    for url in candidates:
        try:
            response = await _request("GET", url, headers={"Accept": "application/json"})
        except GuardedRequestError as exc:
            outcomes.append(f"{url}: {exc}")
            continue
        payload = _json_object(response)
        if payload is None:
            outcomes.append(f"{url}: status {response.status}")
            continue
        try:
            document = _ProtectedResourceDocument.model_validate(payload)
        except ValidationError:
            outcomes.append(f"{url}: not a protected-resource document")
            continue
        if not _resource_matches(document.resource, mcp_url):
            mismatch = True
            outcomes.append(f"{url}: resource {document.resource!r} is not {mcp_url!r} or a parent of it")
            continue
        return _ResourceEntry(document=document, challenge_scopes=challenge.scope if challenge else None)
    if mismatch:
        raise OAuthDiscoveryError("; ".join(outcomes), step="resource-mismatch")
    raise OAuthDiscoveryError(
        f"the server advertises no protected-resource metadata ({'; '.join(outcomes)})", step="resource-metadata"
    )


def _metadata_candidates(issuer: str) -> list[str]:
    split = urlsplit(issuer)
    origin = f"{split.scheme}://{split.netloc}"
    path = _strip_one_slash(split.path)
    if not path:
        return [
            f"{origin}/.well-known/oauth-authorization-server",
            f"{origin}/.well-known/openid-configuration",
        ]
    return [
        f"{origin}/.well-known/oauth-authorization-server{path}",
        f"{origin}/.well-known/openid-configuration{path}",
        f"{_strip_one_slash(issuer)}/.well-known/openid-configuration",
    ]


def _endpoint_allowed(url: str) -> bool:
    split = urlsplit(url)
    if not split.netloc:
        return False
    return split.scheme == "https" or (split.scheme == "http" and is_loopback_host(split.hostname or ""))


def _check_metadata(issuer: str, metadata: _AuthorizationServerMetadata) -> None:
    if not same_issuer(metadata.issuer, issuer):
        raise OAuthDiscoveryError(
            f"metadata issuer {metadata.issuer!r} is not the issuer {issuer!r} it was fetched for",
            step="issuer-mismatch",
        )
    if not metadata.authorization_endpoint or not metadata.token_endpoint:
        raise OAuthDiscoveryError(
            f"metadata of {issuer!r} lacks authorization_endpoint or token_endpoint",
            step="authorization-server-metadata",
        )
    if "S256" not in (metadata.code_challenge_methods_supported or []):
        raise OAuthDiscoveryError(f"{issuer!r} does not advertise PKCE S256", step="pkce-unsupported")
    for name in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
        value = getattr(metadata, name)
        if value is not None and not _endpoint_allowed(value):
            raise OAuthDiscoveryError(
                f"{name} of {issuer!r} must be https, or http on a loopback host: {value!r}",
                step="authorization-server-metadata",
            )


async def _fetch_metadata(issuer: str) -> _AuthorizationServerMetadata:
    outcomes: list[str] = []
    for url in _metadata_candidates(issuer):
        try:
            response = await _request("GET", url, headers={"Accept": "application/json"})
        except GuardedRequestError as exc:
            outcomes.append(f"{url}: {exc}")
            continue
        payload = _json_object(response)
        if payload is None:
            outcomes.append(f"{url}: status {response.status}")
            continue
        try:
            metadata = _AuthorizationServerMetadata.model_validate(payload)
        except ValidationError as exc:
            raise OAuthDiscoveryError(
                f"{url} is not authorization-server metadata ({exc.error_count()} errors)",
                step="authorization-server-metadata",
            ) from exc
        _check_metadata(issuer, metadata)
        return metadata
    raise OAuthDiscoveryError(
        f"no authorization-server metadata for {issuer!r} ({'; '.join(outcomes)})",
        step="authorization-server-metadata",
    )


def _choose_issuer(servers: list[str], bound_issuer: str | None, mcp_url: str) -> str:
    if bound_issuer is None:
        return servers[0]
    for server in servers:
        if same_issuer(server, bound_issuer):
            return server
    raise OAuthDiscoveryError(
        f"{mcp_url!r} does not list the authorization server {bound_issuer!r} (it lists {servers})",
        step="issuer-mismatch",
    )


async def discover(mcp_url: str, *, fresh: bool = False, bound_issuer: str | None = None) -> DiscoveredAuthorization:
    """Discover the authorization server of ``mcp_url``.

    ``bound_issuer``: the issuer the caller is bound to (a descriptor's pin, a grant's or a login's
    issuer); the listed server that equals it is used and, when none does, the call raises
    ``OAuthDiscoveryError(step="issuer-mismatch")``. Unbound, the first listed server. ``fresh``
    refetches both documents. Every failure raises :class:`OAuthDiscoveryError` naming its step.
    """
    now = _monotonic()
    resource_entry = None if fresh else _cached(_resource_cache, mcp_url, now)
    fetched_resource = resource_entry is None
    if resource_entry is None:
        resource_entry = await _fetch_resource(mcp_url)
    document = resource_entry.document
    issuer = _choose_issuer(document.authorization_servers, bound_issuer, mcp_url)
    metadata = None if fresh else _cached(_metadata_cache, issuer, now)
    fetched_metadata = metadata is None
    if metadata is None:
        metadata = await _fetch_metadata(issuer)
    if fetched_resource:
        _resource_cache[mcp_url] = (now + CACHE_TTL_SECONDS, resource_entry)
    if fetched_metadata:
        _metadata_cache[issuer] = (now + CACHE_TTL_SECONDS, metadata)
    authorization_endpoint = metadata.authorization_endpoint
    token_endpoint = metadata.token_endpoint
    if authorization_endpoint is None or token_endpoint is None:
        raise OAuthDiscoveryError(f"metadata of {issuer!r} lacks its endpoints", step="authorization-server-metadata")
    auth_methods = metadata.token_endpoint_auth_methods_supported
    return DiscoveredAuthorization(
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=metadata.registration_endpoint,
        revocation_endpoint=metadata.revocation_endpoint,
        resource=document.resource,
        resource_name=document.resource_name,
        challenge_scopes=resource_entry.challenge_scopes,
        scopes_supported=tuple(document.scopes_supported or ()),
        offline_access_supported="offline_access" in (metadata.scopes_supported or []),
        token_endpoint_auth_methods_supported=tuple(auth_methods) if auth_methods is not None else None,
        client_id_document_supported=metadata.client_id_metadata_document_supported,
        iss_parameter_supported=metadata.authorization_response_iss_parameter_supported,
        authorization_servers=tuple(document.authorization_servers),
    )
