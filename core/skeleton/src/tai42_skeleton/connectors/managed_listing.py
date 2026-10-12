"""The listing half of the managed-credential seam: list a managed entry's tools.

A listing sends the request without a credential first and reaches for a credential only when
the server answers it 401 (:func:`list_with_managed_auth`). It shares the call seam's server check
(:func:`~tai42_skeleton.connectors.token_injection.require_own_server`), its request preparation
(:func:`~tai42_skeleton.connectors.token_injection.prepare_managed_request`) and its scope reader;
it never refreshes in its own loop (a refresh goes to the live refresh owner).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from tai42_contract.connectors.errors import ConnectorError
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.manifest import TaiMCPConfig

from tai42_skeleton.connectors.providers.registry import get_provider_staged
from tai42_skeleton.connectors.runtime import refresh_owner
from tai42_skeleton.connectors.runtime.http_status import mcp_http_status
from tai42_skeleton.connectors.runtime.resolver import (
    ConnectorConnectionError,
    ConnectorReconnectRequiredError,
    ManagedAuth,
)
from tai42_skeleton.connectors.token_injection import (
    _force_refresh,
    _insufficient_scope,
    _require_managed,
    _resolve_for_config,
    _scope_challenge,
    prepare_managed_request,
    require_own_server,
)
from tai42_skeleton.manifest.transport import detect_transport

logger = logging.getLogger(__name__)


@dataclass
class ManagedListing:
    """How a listing opens its one-shot client, and whether its server demanded a credential.

    ``credential_demanded`` is set by :func:`list_with_managed_auth` whatever its outcome: whether the
    request without a credential was answered 401 for a managed entry that talks to a ``url`` and whose
    provider is not one without sign-in.
    """

    open_client: Callable[[TaiMCPConfig], AbstractAsyncContextManager[Any]]
    credential_demanded: bool = False


class ManagedListingRefusedError(Exception):
    """A managed entry's listing refused for want of a usable credential; the message names the connection."""


async def _list_once[T](
    config: TaiMCPConfig, auth: ManagedAuth | None, listing: ManagedListing, run: Callable[[Any], Awaitable[T]]
) -> T:
    effective = config if auth is None else prepare_managed_request(config, auth, "http")[0]
    async with listing.open_client(effective) as client:
        return await run(client)


def _raise_on_listing_scope_challenge(config: TaiMCPConfig, exc: BaseException) -> None:
    """A listing's scope challenge fails it naming the scopes and the reconnect; nothing is written."""
    scopes = _scope_challenge(exc)
    if scopes is not None:
        raise _insufficient_scope(config, scopes) from exc


def _signs_in(config: TaiMCPConfig) -> bool:
    """Whether ``config``'s provider is not known to be one without sign-in (an unknown provider counts)."""
    ref = _require_managed(config)
    try:
        return get_provider_staged(ref.provider_id).kind == "oauth"
    except KeyError:
        return True


def _listing_checked_descriptor(config: TaiMCPConfig) -> ProviderDescriptor | None:
    try:
        return require_own_server(config)
    except ConnectorConnectionError:
        return None


async def list_with_managed_auth[T](
    config: TaiMCPConfig, listing: ManagedListing, run: Callable[[Any], Awaitable[T]]
) -> T:
    """List ``config``'s tools (``run(client)``), sending a credential only when the server demands one.

    1. The request without a credential, exactly as for any entry. It lists → done; any failure other
       than a 401 → raised as it is.
    2. Only after a 401, for a managed entry that talks to a ``url`` and whose provider is not one
       without sign-in: the credential demand is noted, then the entry is checked against its
       provider's server; a refused entry (logged at ERROR) fails with step 1's refusal and nothing
       is resolved.
    3. A credential already stored, read without refreshing (no lock, no write): the request with it.
    4. Otherwise — nothing fresh stored, an error from the read, or step 3 answered 401 — and only with a
       live refresh owner (a listing never refreshes in its own loop): a forced refresh fenced on the
       token step 3 sent, else the ordinary resolve, then the request with it.
    A connector error or a refused fresh credential fails with :class:`ManagedListingRefusedError`
    chained to the 401; a scope challenge (steps 3-4) with :class:`ConnectorInsufficientScopeError`.
    """
    listing.credential_demanded = False
    try:
        return await _list_once(config, None, listing, run)
    except Exception as exc:
        if (
            mcp_http_status(exc) != 401
            or config.managed is None
            or detect_transport(config.config) != "http"
            or not _signs_in(config)
        ):
            raise
        refusal: BaseException = exc
    listing.credential_demanded = True
    if _listing_checked_descriptor(config) is None:
        raise refusal

    stored = await _stored_for_listing(config)
    sent: str | None = None
    if stored is not None:
        try:
            return await _list_once(config, stored, listing, run)
        except Exception as exc:
            _raise_on_listing_scope_challenge(config, exc)
            if mcp_http_status(exc) != 401:
                raise
            logger.warning(
                "connectors: the server of %r refused the stored credential of connection %s",
                config.title,
                _require_managed(config).connection_id,
            )
            sent, refusal = stored.access_token, exc

    if not refresh_owner.is_live():
        raise refusal
    return await _list_with_obtained(config, listing, run, sent=sent, refusal=refusal)


async def _stored_for_listing(config: TaiMCPConfig) -> ManagedAuth | None:
    """A listing's step 3 read: the stored OAuth credential when fresh, read without refreshing, else ``None``.

    A failing read is logged at WARNING and counts as nothing stored (step 4 then obtains one).
    """
    try:
        stored = await _resolve_for_config(config, allow_refresh=False)
    except Exception:
        logger.warning(
            "connectors: listing %r could not read the stored credential of connection %s",
            config.title,
            _require_managed(config).connection_id,
            exc_info=True,
        )
        return None
    return stored if stored is not None and stored.access_token else None


async def _list_with_obtained[T](
    config: TaiMCPConfig,
    listing: ManagedListing,
    run: Callable[[Any], Awaitable[T]],
    *,
    sent: str | None,
    refusal: BaseException,
) -> T:
    """A listing's step 4: obtain a credential the way a call does, then the request with it."""
    ref = _require_managed(config)
    try:
        auth = await (_force_refresh(config, sent) if sent is not None else _resolve_for_config(config))
    except ConnectorError as exc:
        action = ": reconnect the account" if isinstance(exc, ConnectorReconnectRequiredError) else ""
        raise ManagedListingRefusedError(
            f"MCP entry {config.title!r} needs a credential of connection {ref.connection_id} and none could be "
            f"obtained ({exc}){action}"
        ) from refusal
    if auth is None or not auth.access_token:
        raise refusal
    try:
        return await _list_once(config, auth, listing, run)
    except Exception as exc:
        _raise_on_listing_scope_challenge(config, exc)
        if mcp_http_status(exc) == 401:
            raise ManagedListingRefusedError(
                f"the server of MCP entry {config.title!r} refused a just-obtained credential of connection "
                f"{ref.connection_id}: reconnect the account"
            ) from exc
        raise
