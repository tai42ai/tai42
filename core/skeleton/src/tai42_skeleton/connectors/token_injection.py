"""Connector-specific auth glue for managed MCP requests: the one seam calls and listings go through.

The general tool-call dispatch lives in
``tai42_skeleton.tools.adapters.mcp_tool_to_func``; this module holds the parts that
are specific to connector-managed entries: the check that a credential goes only to
its provider's own server, resolving a ``ManagedAuth`` from the runtime store and
injecting it into the request — an OAuth token (http Bearer header or stdio ``_meta``
field) or, for a no-auth connection, the client's static config (http headers or
stdio ``env``) — recognising the hub's structured error sentinel, an HTTP 401 and a
scope challenge, and the force-refresh-and-retry-once flow of a call
(:func:`with_managed_auth`). A listing's order over the same check, preparation and
scope reader lives in :mod:`tai42_skeleton.connectors.managed_listing`.

``is_managed`` (immutable on the config) gates every connector branch. The
empty-token guard (OAuth-only — a no-auth credential legitimately has no token),
the anchored error-framing regex (anti-forgery), the transport allowlist, and
the single-retry semantics are load-bearing.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

import mcp
from mcp.shared.exceptions import McpError
from tai42_contract.connectors.models import ALIAS_RE, ConnectorRef
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.errors import ClientDisconnectedError
from tai42_contract.manifest import TaiMCPConfig
from tai42_kit.clients.impl.mcp import FastMCPClient

from tai42_skeleton.connectors.oauth.discovery import parse_www_authenticate
from tai42_skeleton.connectors.providers.registry import get_provider_staged
from tai42_skeleton.connectors.runtime.http_status import mcp_http_response, mcp_http_status
from tai42_skeleton.connectors.runtime.launch import SUPPORTED_MANAGED_TRANSPORTS
from tai42_skeleton.connectors.runtime.own_server import own_server_refusal
from tai42_skeleton.connectors.runtime.resolver import (
    ConnectorAuthExpiredError,
    ConnectorConnectionError,
    ConnectorInsufficientScopeError,
    ConnectorReconnectRequiredError,
    ConnectorRefreshFailingError,
    ManagedAuth,
    force_refresh,
    note_scopes_wanted,
    resolve_managed_auth,
)
from tai42_skeleton.connectors.service.manifest_writer import managed_title
from tai42_skeleton.connectors.settings import connector_adapter_settings
from tai42_skeleton.manifest.transport import detect_transport
from tai42_skeleton.settings.mcp_settings import mcp_dispatch_settings

logger = logging.getLogger(__name__)


_TOKEN_EXPIRED_CODE = "token_expired"  # noqa: S105 constant identifier, not a secret value

# Outbound structured-error codes — the client-facing half of the connector-error
# wire contract. When a managed tool call cannot proceed until the user acts, the
# adapter frames one of these (same prefix + ``{"code": ...}`` JSON the connector
# servers use) so a client recovers a machine-actionable signal via
# :func:`extract_connector_error_payload` instead of only a generic error string.
_RECONNECT_REQUIRED_CODE = "reconnect_required"
_REFRESH_FAILING_CODE = "refresh_failing"
_AUTH_EXPIRED_CODE = "auth_expired"
_INSUFFICIENT_SCOPE_CODE = "insufficient_scope"
_UPSTREAM_MCP_UNAVAILABLE_CODE = "upstream_mcp_unavailable"
_MCP_DISPATCH_FAILED_CODE = "mcp_dispatch_failed"


class McpDispatchError(Exception):
    """The remote MCP tool call was reached but did not yield a usable result.

    Raised only from the call seam (:func:`with_managed_auth`) — the single point that performs the
    remote ``tools/call`` — for a protocol error / timeout (``McpError``) or a response that violated
    the tool's advertised output schema (the calling SDK's validation ``RuntimeError``). It
    carries the entry title and tool name and chains the original failure as ``__cause__``.

    A dedicated type so the dispatch seam types exactly the remote-call failures into a
    structured tool-error result, while a local misconfiguration (an empty resolved token, a
    missing connector ref), a force-refresh failure, or a programming error — none of which are
    remote-call failures — still propagate raw and raise loudly. A lost/evicted session raises
    :class:`ClientDisconnectedError` instead, which the one-shot reconnect handles first.
    """

    def __init__(self, title: str, tool_name: str) -> None:
        """Record the MCP entry ``title`` and the ``tool_name`` whose remote call failed."""
        super().__init__(f"remote MCP tool call {tool_name!r} on {title!r} failed")
        self.title = title
        self.tool_name = tool_name


# The ``_meta`` token key and error prefix are the cross-repo wire contract with
# the connector-launched servers. They are read from settings lazily (never at
# import), so a ``.env`` override applied by the CLI bootstrap before the first
# tool call takes effect — settings_cache memoises the value after that.
def _meta_token_key() -> str:
    return connector_adapter_settings().meta_token_key


def _error_prefix() -> str:
    return connector_adapter_settings().error_prefix


_FRAMING_RE_CACHE: re.Pattern[str] | None = None


def _connector_error_framing_re() -> re.Pattern[str]:
    """Compiled (and memoised) framing regex for the error sentinel.

    Match the error prefix only at start-of-text or right after fastmcp's
    ``Error calling tool '<name>': `` framing — so a managed tool echoing user
    input can't forge the sentinel and trigger a phantom retry. Built on first
    use, not at import, so the prefix is read post-bootstrap.
    """
    global _FRAMING_RE_CACHE
    if _FRAMING_RE_CACHE is None:
        _FRAMING_RE_CACHE = re.compile(
            rf"^(?:Error calling tool '[^']+': )?{re.escape(_error_prefix())}",
        )
    return _FRAMING_RE_CACHE


def __getattr__(name: str) -> object:
    # Expose the settings-backed wire values as module attributes for importers
    # (``from token_injection import CONNECTOR_ERROR_PREFIX``) while deferring the
    # settings read / regex build to first access (after the CLI env bootstrap)
    # rather than running it at import — mirrors ``app.instance``'s lazy ``app``.
    if name == "CONNECTOR_META_TOKEN_KEY":
        return _meta_token_key()
    if name == "CONNECTOR_ERROR_PREFIX":
        return _error_prefix()
    if name == "_CONNECTOR_ERROR_FRAMING_RE":
        return _connector_error_framing_re()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# -- The provider's own server -------------------------------------------------


def _builder_title(config: TaiMCPConfig, ref: ConnectorRef) -> bool:
    """Whether ``config``'s title has the shape the managed-entry builder gives ``ref``'s sub-service."""
    prefix = managed_title(ref.provider_id, ref.sub_service, "")
    return config.title.startswith(prefix) and ALIAS_RE.match(config.title[len(prefix) :]) is not None


def require_own_server(config: TaiMCPConfig) -> ProviderDescriptor | None:
    """Refuse a managed entry whose ``url`` is not its provider's own server, before any credential is touched.

    Returns the descriptor the entry was checked against, or ``None`` when the entry is not checked (not
    managed, or not talking to a ``url``: a stdio entry's token goes to a process launched from the
    entry's own command). The descriptor comes from the registry generation in force where this runs.
    A refusal is logged at ERROR and raised as :class:`ConnectorConnectionError`, whose message names
    the entry and the connection and says what to do.
    """
    ref = config.managed
    if ref is None or detect_transport(config.config) != "http":
        return None
    try:
        descriptor: ProviderDescriptor | None = get_provider_staged(ref.provider_id)
    except KeyError:
        descriptor = None
    reason = own_server_refusal(config, descriptor)
    if reason is None:
        return descriptor
    entry = f"MCP entry {config.title!r} carries the reference of connection {ref.connection_id} but {reason}"
    if not _builder_title(config, ref):
        message = (
            f"{entry}; this entry is not the connector's own, and a connector's credential is sent only to the "
            "connector's server: remove the reference from the entry"
        )
    elif descriptor is not None and descriptor.kind == "none":
        message = f"{entry}; the connector's own entry fell behind its provider: save the connection's services again"
    else:
        message = f"{entry}; the connector's own entry fell behind its provider: reconnect the account"
    logger.error("connectors: %s", message)
    raise ConnectorConnectionError(message, connection_id=ref.connection_id)


# -- Managed auth -------------------------------------------------------------


async def resolve_managed_auth_for_config(config: TaiMCPConfig) -> ManagedAuth | None:
    """Resolve a ManagedAuth for a managed config, or None.

    None means "inject nothing": a non-managed config, or a no-auth managed entry
    with no client config. A no-auth entry WITH config returns a ManagedAuth
    carrying headers/env (no token). Only an OAuth result (``access_token`` set)
    is checked for an empty token — a no-auth credential legitimately has none.
    The entry is checked against its provider's server first (:func:`require_own_server`).
    """
    require_own_server(config)
    return await _resolve_for_config(config)


async def _resolve_for_config(config: TaiMCPConfig, *, allow_refresh: bool = True) -> ManagedAuth | None:
    m = config.managed
    if m is None:  # is_managed ≡ managed is not None
        return None
    auth = await resolve_managed_auth(m.connection_id, m.provider_id, m.sub_service, allow_refresh=allow_refresh)
    if auth is not None and auth.access_token is not None:
        _reject_empty_token(auth, m)
    return auth


async def _force_refresh(config: TaiMCPConfig, failed_access_token: str | None) -> ManagedAuth:
    require_own_server(config)
    m = _require_managed(config)
    auth = await force_refresh(m.connection_id, failed_access_token=failed_access_token)
    _reject_empty_token(auth, m)
    return auth


def _require_managed(config: TaiMCPConfig) -> ConnectorRef:
    """The connector ref of a managed entry.

    The connector branches are gated on ``is_managed``, so a missing ref here is a caller bug that fails loudly.
    """
    if config.managed is None:
        raise RuntimeError(f"config {config.title!r} is not connector-managed")
    return config.managed


def _reject_empty_token(auth: ManagedAuth, m) -> None:
    """Refuse a ManagedAuth with no access_token — never send an unauthenticated request as if it were authenticated."""
    if not auth.access_token:
        raise RuntimeError(
            "resolved ManagedAuth has empty access_token for "
            f"connection_id={m.connection_id} sub_service={m.sub_service}",
        )


# -- Token injection ----------------------------------------------------------


def _merge_http_auth(config: TaiMCPConfig, auth: ManagedAuth) -> TaiMCPConfig:
    """Return a copy of ``config`` with ``Authorization: Bearer …`` merged in.

    Header keys are lowercased to dedupe against any manifest-supplied header
    (HTTP names are case-insensitive, dicts are not).
    """
    raw_headers = config.config.headers or {}
    merged = {k.lower(): v for k, v in raw_headers.items()}
    merged["authorization"] = f"Bearer {auth.access_token}"
    new_inner = config.config.model_copy(update={"headers": merged})
    return config.model_copy(update={"config": new_inner})


def _merge_http_headers(config: TaiMCPConfig, headers: dict[str, str]) -> TaiMCPConfig:
    """Return a copy of ``config`` with the client's no-auth ``headers`` merged in (http transport).

    Keys are lowercased to dedupe against manifest-supplied headers.
    """
    raw_headers = config.config.headers or {}
    merged = {k.lower(): v for k, v in raw_headers.items()}
    for key, value in headers.items():
        merged[key.lower()] = value
    new_inner = config.config.model_copy(update={"headers": merged})
    return config.model_copy(update={"config": new_inner})


def _merge_stdio_env(config: TaiMCPConfig, env: dict[str, str]) -> TaiMCPConfig:
    """Return a copy of ``config`` with the client's no-auth ``env`` merged into the stdio launch env.

    Client values override static descriptor env.
    """
    raw_env = config.config.env or {}
    merged = {**raw_env, **env}
    new_inner = config.config.model_copy(update={"env": merged})
    return config.model_copy(update={"config": new_inner})


def extract_connector_error_payload(response: mcp.types.CallToolResult) -> dict | None:
    """Return the connector structured-error payload, or None.

    Connector-launched servers wrap a JSON ``{"code": ...}`` payload with
    ``CONNECTOR_ERROR_PREFIX``. Only the anchored framing is trusted, so an
    echoed-back prefix in a tool's error string can't forge a match. Caller
    MUST gate on ``config.is_managed``.
    """
    if not response.isError:
        return None
    for block in response.content or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        m = _connector_error_framing_re().match(text)
        if m is None:
            continue
        suffix = text[m.end() :].strip()
        try:
            payload = json.loads(suffix)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("code"), str):
            return payload
    return None


def managed_auth_error_result(
    exc: ConnectorConnectionError | ConnectorAuthExpiredError | ConnectorInsufficientScopeError,
) -> mcp.types.CallToolResult:
    """Build an error ``CallToolResult`` for a managed tool call blocked on user action.

    Covers ``invalid_grant`` (reconnect), refresh budget exhausted, auth still expired after a forced
    refresh, an entry refused its credential, and a server's scope challenge (``insufficient_scope``,
    carrying the scopes the challenge named).

    The text carries the connector-error prefix + a ``{"code": ...}`` payload, so
    it surfaces through the same error channel as a sub-server connector error and
    a client recovers the reconnect signal via
    :func:`extract_connector_error_payload`; it also reads cleanly as prose.
    """
    if isinstance(exc, ConnectorReconnectRequiredError):
        code = _RECONNECT_REQUIRED_CODE
    elif isinstance(exc, ConnectorRefreshFailingError):
        code = _REFRESH_FAILING_CODE
    elif isinstance(exc, ConnectorAuthExpiredError):
        code = _AUTH_EXPIRED_CODE
    elif isinstance(exc, ConnectorInsufficientScopeError):
        code = _INSUFFICIENT_SCOPE_CODE
    else:  # a bare ConnectorConnectionError (the connection is missing, or the entry is refused)
        code = _RECONNECT_REQUIRED_CODE
    payload: dict[str, Any] = {"code": code, "message": str(exc), "connection_id": exc.connection_id}
    for attr in ("provider_id", "sub_service"):  # present on the expired and scope errors
        value = getattr(exc, attr, None)
        if value is not None:
            payload[attr] = value
    if isinstance(exc, ConnectorInsufficientScopeError):
        payload["scopes"] = list(exc.scopes)
    text = f"{_error_prefix()}{json.dumps(payload)}"
    return mcp.types.CallToolResult(isError=True, content=[mcp.types.TextContent(type="text", text=text)])


def upstream_mcp_unavailable_result(config: TaiMCPConfig) -> mcp.types.CallToolResult:
    """Build an error ``CallToolResult`` for an upstream MCP whose pooled session died.

    It did not come back on the one-shot reconnect.

    Mirrors :func:`managed_auth_error_result`'s envelope — the connector-error
    prefix + a ``{"code": ...}`` payload recoverable via
    :func:`extract_connector_error_payload`. The consumer-facing message names
    the MCP entry by its configured title; the raw disconnect text is never the
    message (it is logged at the dispatch seam).
    """
    message = (
        f"the upstream MCP {config.title!r} connection was lost and reconnection "
        "failed; the tool call could not be completed"
    )
    payload = {"code": _UPSTREAM_MCP_UNAVAILABLE_CODE, "message": message}
    text = f"{_error_prefix()}{json.dumps(payload)}"
    return mcp.types.CallToolResult(isError=True, content=[mcp.types.TextContent(type="text", text=text)])


def mcp_dispatch_failed_result(config: TaiMCPConfig, tool_name: str) -> mcp.types.CallToolResult:
    """Build an error ``CallToolResult`` for a reachable upstream whose tool call did not yield a usable result.

    Covers a protocol error / timeout (``McpError``) and a response that violated the tool's
    own advertised output schema (the calling SDK's validation ``RuntimeError``) — the upstream
    answered (or was reached) but the call failed, which is distinct from an unreachable pooled
    session (:func:`upstream_mcp_unavailable_result`) and from an auth block
    (:func:`managed_auth_error_result`).

    Mirrors their envelope — the connector-error prefix + a ``{"code": ...}`` payload recoverable
    via :func:`extract_connector_error_payload`. The consumer-facing message names the tool and
    the MCP entry by its configured title; the raw exception text (SDK/schema-validator internals)
    is NEVER the message — it is logged at the dispatch seam and kept in the health store's
    last_error, both operator surfaces.
    """
    message = (
        f"the tool {tool_name!r} on MCP {config.title!r} could not be completed: the upstream "
        "server returned an error or an invalid response"
    )
    payload = {"code": _MCP_DISPATCH_FAILED_CODE, "message": message}
    text = f"{_error_prefix()}{json.dumps(payload)}"
    return mcp.types.CallToolResult(isError=True, content=[mcp.types.TextContent(type="text", text=text)])


def check_managed_transport(config: TaiMCPConfig, transport: str) -> None:
    """Allow managed entries only on transports with a token-injection path."""
    if config.is_managed and transport not in SUPPORTED_MANAGED_TRANSPORTS:
        raise RuntimeError(
            f"managed transport {transport!r} is not supported; connector-"
            "managed entries must be http (header auth) or stdio (_meta auth)."
        )


def prepare_managed_request(
    config: TaiMCPConfig,
    auth: ManagedAuth | None,
    transport: str,
) -> tuple[TaiMCPConfig, dict | None]:
    """Return ``(effective_config, meta)`` for one dispatch.

    ``auth is None`` → inject nothing: a non-managed entry OR a no-auth managed
    entry with no client config. ``auth.access_token`` set → OAuth: stdio ``_meta``
    token / http Bearer header. ``auth`` with headers/env but no token → no-auth
    managed with client config: merge the client's headers (http) or env (stdio).
    """
    if auth is None:
        return config, None
    if auth.access_token is not None:
        # OAuth managed entry.
        if transport == "stdio":
            return config, {_meta_token_key(): auth.access_token}
        # transport == "http" — unsupported transports rejected by the pre-flight.
        return _merge_http_auth(config, auth), None
    # No-auth managed entry with client config — transport-matched (env for stdio,
    # headers for http); MCPConfig rejects the cross combination.
    if transport == "stdio":
        return _merge_stdio_env(config, auth.env), None
    return _merge_http_headers(config, auth.headers), None


def is_token_expired(payload: dict | None) -> bool:
    """True if a structured-error payload carries the token_expired code."""
    return payload is not None and payload.get("code") == _TOKEN_EXPIRED_CODE


# -- The seam: every managed request is prepared and its auth failure handled here --

# An RFC 6749 scope token (section 3.3 ``scope-token``).
_SCOPE_TOKEN_RE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]+$")


class _TokenExpired:
    """A request's outcome meaning the access token it carried has expired (a 401, or the marker)."""

    def __init__(self, cause: BaseException | None) -> None:
        self.cause = cause


def _sent_token(config: TaiMCPConfig, auth: ManagedAuth | None) -> str | None:
    """The access token a request of ``config`` carries: a managed OAuth credential's, else ``None``."""
    if config.managed is None or auth is None or not auth.access_token:
        return None
    return auth.access_token


def _scope_challenge(exc: BaseException) -> list[str] | None:
    """The scopes of a 403 ``insufficient_scope`` Bearer challenge in ``exc``'s chain, or ``None``.

    The scopes are the challenge's ``scope`` in its order (possibly none); a token outside the RFC 6749
    scope-token grammar is dropped with a WARNING.
    """
    response = mcp_http_response(exc)
    if response is None or response.status_code != 403:
        return None
    headers = getattr(response, "headers", None)
    challenge = parse_www_authenticate(headers.get("www-authenticate") if headers is not None else None)
    if challenge is None or challenge.error != "insufficient_scope":
        return None
    scopes: list[str] = []
    for token in challenge.scope or ():
        if _SCOPE_TOKEN_RE.match(token):
            scopes.append(token)
        else:
            logger.warning("connectors: dropped a scope token outside the RFC 6749 grammar: %r", token)
    return list(dict.fromkeys(scopes))


def _insufficient_scope(config: TaiMCPConfig, scopes: list[str]) -> ConnectorInsufficientScopeError:
    ref = _require_managed(config)
    return ConnectorInsufficientScopeError(
        connection_id=ref.connection_id, provider_id=ref.provider_id, sub_service=ref.sub_service, scopes=scopes
    )


async def _close_session(mcp_client: FastMCPClient, dump: dict, ended_by: BaseException | None, what: str) -> None:
    """Close the pooled session keyed by ``dump``; a failing close is logged and never replaces the outcome.

    Closing a session that an HTTP refusal ended re-raises that refusal: it is logged at INFO, any
    other close failure at WARNING.
    """
    try:
        await mcp_client.close(config=dump)
    except Exception as exc:
        status = mcp_http_status(exc)
        if ended_by is not None and status is not None and status == mcp_http_status(ended_by):
            logger.info("closed the pooled MCP session %s; it had ended with HTTP %s", what, status)
        else:
            logger.warning("failed to close the pooled MCP session %s", what, exc_info=True)


async def _on_scope_challenge(
    config: TaiMCPConfig, dump: dict, mcp_client: FastMCPClient, scopes: list[str], exc: BaseException
) -> NoReturn:
    """A call's scope challenge: end its session, note the scopes for the next reconnect, raise the outcome.

    No refresh and no second request (a refresh cannot widen a grant). A store error while noting is
    logged at ERROR and does not change the call's answer.
    """
    ref = _require_managed(config)
    await _close_session(mcp_client, dump, exc, f"of {config.title!r} that carried a scope challenge")
    if scopes:
        try:
            await note_scopes_wanted(ref.connection_id, scopes)
        except Exception:
            logger.error(
                "connectors: noting the scopes connection %s wants failed; the call still answers insufficient_scope",
                ref.connection_id,
                exc_info=True,
            )
    raise _insufficient_scope(config, scopes) from exc


async def _call_once(
    config: TaiMCPConfig,
    auth: ManagedAuth | None,
    transport: str,
    mcp_client: FastMCPClient,
    run: Callable[[Any, dict | None], Awaitable[mcp.types.CallToolResult]],
    tool_name: str,
) -> mcp.types.CallToolResult | _TokenExpired:
    """One request of a call, its outcome classified before any error-kind reading.

    A 401 in the failure's chain, or the ``token_expired`` marker in the result, of a request that
    carried a managed access token is "token expired" — also when the client wrapped the 401 as a
    connect failure. A 403 scope challenge raises :class:`ConnectorInsufficientScopeError`. A lost or
    unbuildable session raises :class:`ClientDisconnectedError` (the one-shot reconnect handles it);
    any other failure of the remote call — a protocol error / timeout (``McpError``) or a response that
    violated the tool's advertised output schema (the calling SDK's validation ``RuntimeError``) — is
    wrapped as :class:`McpDispatchError`.
    """
    effective, meta = prepare_managed_request(config, auth, transport)
    dump = effective.model_dump()
    token = _sent_token(config, auth)
    try:
        async with mcp_client.current(config=dump) as client:
            result = await run(client, meta)
    except Exception as exc:
        if token is not None:
            if mcp_http_status(exc) == 401:
                return _TokenExpired(exc)
            scopes = _scope_challenge(exc)
            if scopes is not None:
                await _on_scope_challenge(config, dump, mcp_client, scopes, exc)
        if isinstance(exc, ClientDisconnectedError):
            raise
        if isinstance(exc, (McpError, RuntimeError)):
            raise McpDispatchError(config.title, tool_name) from exc
        raise
    if token is not None and is_token_expired(extract_connector_error_payload(result)):
        return _TokenExpired(None)
    return result


async def with_managed_auth(
    config: TaiMCPConfig,
    transport: str,
    mcp_client: FastMCPClient,
    run: Callable[[Any, dict | None], Awaitable[mcp.types.CallToolResult]],
    *,
    tool_name: str,
) -> mcp.types.CallToolResult:
    """Run one tool call of ``config`` with its managed credential: the only place a call is prepared.

    ``run(client, meta)`` is the request on the pooled session. The entry is checked against its
    provider's server first, then the credential is resolved and merged (:func:`prepare_managed_request`).
    "Token expired" (:func:`_call_once`) force-refreshes fenced on the failing token, runs ONCE more with
    the fresh credential, then evicts the superseded pooled session when its key differs; a second
    "token expired" raises :class:`ConnectorAuthExpiredError`. A 401 for a non-managed or no-auth entry is
    not touched, and a 403 is never treated as expiry.
    """
    require_own_server(config)
    auth = await _resolve_for_config(config)
    outcome = await _call_once(config, auth, transport, mcp_client, run, tool_name)
    if not isinstance(outcome, _TokenExpired):
        return outcome
    managed = _require_managed(config)
    logger.info(
        "connectors: token expired from %s/%s (connection_id=%s) — force-refreshing and retrying once",
        managed.provider_id,
        managed.sub_service,
        managed.connection_id,
    )
    refreshed = await _force_refresh(config, _sent_token(config, auth))
    retried: mcp.types.CallToolResult | _TokenExpired | None = None
    try:
        retried = await _call_once(config, refreshed, transport, mcp_client, run, tool_name)
        if isinstance(retried, _TokenExpired):
            raise ConnectorAuthExpiredError(
                connection_id=managed.connection_id,
                provider_id=managed.provider_id,
                sub_service=managed.sub_service,
            ) from retried.cause
        return retried
    finally:
        # The fresh session served the retry, so the superseded one is evicted whatever the retry's
        # outcome — but only by a genuinely distinct key (a rotated http header): on stdio the token
        # travels in ``_meta``, the key is unchanged, and closing it would evict the live session.
        superseded = prepare_managed_request(config, auth, transport)[0].model_dump()
        if superseded != prepare_managed_request(config, refreshed, transport)[0].model_dump():
            await _close_session(mcp_client, superseded, outcome.cause, "superseded by a token rotation")


async def call_with_auth(
    config: TaiMCPConfig,
    transport: str,
    tool_name: str,
    arguments: Any,
    mcp_client: FastMCPClient,
) -> mcp.types.CallToolResult:
    """One MCP tool call through :func:`with_managed_auth`, under the settings-backed call-time budget."""

    async def _call(client: Any, meta: dict | None) -> mcp.types.CallToolResult:
        return await client.call_tool_mcp(
            tool_name,
            arguments,
            meta=meta,
            timeout=mcp_dispatch_settings().call_timeout_seconds,
        )

    return await with_managed_auth(config, transport, mcp_client, _call, tool_name=tool_name)


async def evict_pooled_session(
    config: TaiMCPConfig,
    transport: str,
    mcp_client: FastMCPClient,
) -> None:
    """Drop the pooled MCP session a dispatch of ``config`` would reuse.

    The key is computed from the credential that is STORED, read without refreshing, through the same
    preparation the call seam keys its pool on: with a fresh stored credential it is the key dispatch
    uses; with none, the plain key. An entry refused by :func:`require_own_server` gets the plain key
    of the entry as written. A probe passes on a throwaway off-pool client, so a dead pooled session
    survives it; closing it by its exact key forces the next dispatch to build a fresh session. The
    read or the close is allowed to raise — a failed eviction surfaces as the reload op's failure. A
    session keyed by a token since replaced is not this call's target: the dispatch path's disconnect
    handling closes it.
    """
    try:
        require_own_server(config)
    except ConnectorConnectionError:
        await mcp_client.close(config=config.model_dump())
        return
    auth = await _resolve_for_config(config, allow_refresh=False)
    effective_config, _meta = prepare_managed_request(config, auth, transport)
    await mcp_client.close(config=effective_config.model_dump())
