"""The ``WWW-Authenticate`` pointer on every 401 an MCP endpoint answers — one chokepoint.

:class:`McpChallengeMiddleware` is the outermost member of the access-control stack, so
every refusal produced beneath it passes through it: the credential backend's 401, the
resource guard's 401, and the MCP library's own inner gate. On a 401 for a request whose
canonical path addresses an MCP endpoint, while the deployment advertises an
authorization server, it guarantees the RFC 9728 pointer to that endpoint's
protected-resource metadata document::

    WWW-Authenticate: Bearer resource_metadata="<public base URL>/.well-known/oauth-protected-resource<endpoint>"

It appends ``resource_metadata`` to an existing ``Bearer`` challenge that lacks one, leaves
one that carries it untouched, and adds the header when no ``Bearer`` challenge is present.
Status, body and every other header are untouched, as is every response that is not a
401 and every 401 on a path that is not an MCP endpoint. The header names only the
deployment's configured public URL and the endpoint the caller itself addressed.

The challenge names no ``scope``: authorization here is by the subject's policy, not by
OAuth scopes, so there is none to name.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tai42_skeleton.access_control.advertised import advertised_authorization_servers, mcp_metadata_url
from tai42_skeleton.access_control.path_canon import MalformedPathError, request_canonical_path
from tai42_skeleton.app.mcp_endpoints import mcp_endpoint_for

_WWW_AUTHENTICATE = b"www-authenticate"
_RESOURCE_METADATA = "resource_metadata="


class McpChallengeMiddleware:
    """Pure ASGI wrapper adding the protected-resource metadata pointer to an MCP endpoint's 401."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``, the rest of the access-control stack and everything beneath it."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request through, rewriting only the start of a 401 response from an MCP endpoint."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_challenge(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 401:
                message = _with_challenge(scope, message)
            await send(message)

        await self.app(scope, receive, send_with_challenge)


def _with_challenge(scope: Scope, message: Message) -> Message:
    try:
        canonical = request_canonical_path(scope)
    except MalformedPathError:
        return message
    endpoint = mcp_endpoint_for(canonical)
    if endpoint is None or not advertised_authorization_servers():
        return message
    pointer = f'{_RESOURCE_METADATA}"{mcp_metadata_url(endpoint)}"'
    headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
    for index, (name, raw_value) in enumerate(headers):
        if name.lower() != _WWW_AUTHENTICATE:
            continue
        value = raw_value.decode("latin-1")
        scheme, _, params = value.strip().partition(" ")
        if scheme.lower() != "bearer":
            continue
        if _RESOURCE_METADATA in params:
            return message
        challenge = f"{scheme} {params}, {pointer}" if params.strip() else f"{scheme} {pointer}"
        headers[index] = (name, challenge.encode("latin-1"))
        return {**message, "headers": headers}
    headers.append((_WWW_AUTHENTICATE, f"Bearer {pointer}".encode("latin-1")))
    return {**message, "headers": headers}
