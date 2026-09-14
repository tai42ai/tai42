"""Start an OAuth authorize flow: persist the signed :class:`OAuthFlowState` and build
the provider authorize URL."""

from __future__ import annotations

import uuid

from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.connectors.service import StartConnectResult

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.oauth import redirect, state


async def _start_flow(
    *,
    descriptor: ProviderDescriptor,
    alias: str,
    enabled_sub_services: list[str],
    requested_scopes: list[str],
    return_url: str,
    redirect_uri: str,
    origin: str,
    operation: state.FlowOperation,
    reconnect_connection_id: str | None,
) -> StartConnectResult:
    """Persist an OAuthFlowState and build the provider authorize URL.

    ``state`` is a signed envelope carrying the single-use ``flow_id`` (CSRF
    guard) and ``origin`` — this deployment's own origin, which a callback routed
    through the central OAuth bridge reads to bounce the code back here. The origin
    is validated against the redirect allow-list before it is signed, so a spoofed
    ``Origin`` header cannot mint a deployment-signed state pointing off-list.
    """
    redirect.validate_origin_allowed(origin)
    verifier, challenge = oauth_client.generate_pkce_pair()
    flow_id = str(uuid.uuid4())
    flow_state = state.OAuthFlowState(
        flow_id=flow_id,
        provider_id=descriptor.id,
        alias=alias,
        requested_scopes=requested_scopes,
        enabled_sub_services=list(enabled_sub_services),
        pkce_verifier=verifier,
        return_url=return_url,
        redirect_uri=redirect_uri,
        operation=operation,
        reconnect_connection_id=reconnect_connection_id,
    )
    await state.put(flow_state)

    authorize_url = oauth_client.build_authorize_url(
        descriptor=descriptor,
        scopes=requested_scopes,
        state=state.encode(flow_id=flow_id, origin=origin),
        code_challenge=challenge,
        redirect_uri=redirect_uri,
    )
    return StartConnectResult(flow_id=flow_id, authorize_url=authorize_url)
