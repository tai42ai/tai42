"""Start an OAuth authorize flow: persist the signed :class:`OAuthFlowState` and build the authorize URL."""

from __future__ import annotations

import uuid

from tai42_contract.connectors.models import ConnectionRecord
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.connectors.service import StartConnectResult

from tai42_skeleton.connectors.oauth import authorization, redirect, state
from tai42_skeleton.connectors.oauth import client as oauth_client


def _flow_redirect_uri(descriptor: ProviderDescriptor, *, redirect_uri: str, origin: str) -> str:
    """The redirect URI of a flow, chosen by the provider's client kind.

    A provider with an env client uses the address its caller computed (the central bridge when one
    is configured). A stored-client provider always uses this deployment's own callback: the bridge
    forwards a code to whichever origin the ``state`` names, which is safe only for a client whose
    code is useless without its secret.
    """
    if descriptor.client_id_env:
        return redirect_uri
    return redirect.callback_uri(origin)


async def _start_flow(
    *,
    descriptor: ProviderDescriptor,
    alias: str,
    enabled_sub_services: list[str],
    return_url: str,
    redirect_uri: str,
    origin: str,
    operation: state.FlowOperation,
    reconnect_connection_id: str | None,
    record: ConnectionRecord | None = None,
) -> StartConnectResult:
    """Persist an OAuthFlowState and build the authorize URL.

    ``state`` is a signed envelope carrying the single-use ``flow_id`` (CSRF
    guard) and ``origin`` — this deployment's own origin, which a callback routed
    through the central OAuth bridge reads to bounce the code back here. The origin
    and the flow's redirect URI are validated against the redirect allow-list before
    anything is discovered or stored, so a spoofed ``Origin`` header cannot mint a
    deployment-signed state pointing off-list. The authorization and the client are
    resolved before the flow record is written, which records the issuer of the
    validated metadata. ``record`` is the connection a reconnect or toggle re-consents.
    """
    redirect.validate_origin_allowed(origin)
    flow_redirect_uri = oauth_client.validate_redirect_uri(
        _flow_redirect_uri(descriptor, redirect_uri=redirect_uri, origin=origin)
    )
    resolved = await authorization.resolve_authorization(descriptor)
    client = await authorization.resolve_client(descriptor, resolved, redirect_uri=flow_redirect_uri)
    scopes = authorization.requested_scopes(descriptor, enabled_sub_services, resolved, record=record)

    verifier, challenge = oauth_client.generate_pkce_pair()
    flow_id = str(uuid.uuid4())
    flow_state = state.OAuthFlowState(
        flow_id=flow_id,
        provider_id=descriptor.id,
        alias=alias,
        requested_scopes=scopes,
        enabled_sub_services=list(enabled_sub_services),
        pkce_verifier=verifier,
        return_url=return_url,
        redirect_uri=flow_redirect_uri,
        operation=operation,
        reconnect_connection_id=reconnect_connection_id,
        issuer=resolved.issuer,
        resource=resolved.resource,
        client_id=None if client.source == "env" else client.client_id,
        iss_required=resolved.iss_parameter_supported,
    )
    await state.put(flow_state)

    authorize_url = oauth_client.build_authorize_url(
        descriptor=descriptor,
        resolved=resolved,
        client=client,
        scopes=scopes,
        state=state.encode(flow_id=flow_id, origin=origin),
        code_challenge=challenge,
        redirect_uri=flow_redirect_uri,
    )
    return StartConnectResult(flow_id=flow_id, authorize_url=authorize_url)
