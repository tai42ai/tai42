"""What the MCP door advertises: the authorization servers MCP clients sign in at, and the door's public URLs.

The door is on only when BOTH hold: an active identity provider declares an authorization
server (``IdentityProvider.authorization_servers``) and ``ACCESS_CONTROL_PUBLIC_BASE_URL``
is set. :func:`advertised_authorization_servers` is the one definition of that switch: the
metadata documents, the 401 pointer and the request resource all ask it. The two URL
builders are the only places a public URL of the door is composed; nothing reads a
request's own host or scheme.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from urllib.parse import urlsplit

from tai42_contract.access_control.identity import IdentityProvider
from tai42_contract.app import tai42_app

from tai42_skeleton.access_control.settings import access_control_settings, is_loopback_host
from tai42_skeleton.app.reload_gate import reload_gate

logger = logging.getLogger(__name__)

# The RFC 9728 well-known prefix of a protected resource's metadata document.
PROTECTED_RESOURCE_METADATA_PREFIX = "/.well-known/oauth-protected-resource"


def advertised_authorization_servers() -> tuple[str, ...]:
    """The authorization servers this deployment advertises to MCP clients, in order, without duplicates.

    Empty when access control is off or ``public_base_url`` is unset; otherwise the
    declarations of the active identity providers, read the way the token verifier reads
    them. A provider missing while a reload swaps the providers contributes nothing (the
    request beneath is answered with the retriable reload envelope); one missing outside a
    reload is a fault and raises.
    """
    settings = access_control_settings()
    if not settings.enable or settings.public_base_url is None:
        return ()
    servers: dict[str, None] = {}
    for name in settings.resolved_auth_providers():
        provider = tai42_app.accounts.active_provider(name)
        if provider is None:
            if reload_gate.locked:
                continue
            raise RuntimeError(f"identity provider {name!r} is not registered; no reload is in progress")
        servers.update(dict.fromkeys(provider.authorization_servers()))
    return tuple(servers)


def mcp_resource_url(endpoint_path: str) -> str:
    """The public URL of the MCP endpoint at ``endpoint_path`` — the protected resource a client signs in for."""
    return f"{_public_base_url()}{endpoint_path}"


def mcp_metadata_url(endpoint_path: str) -> str:
    """The public URL of the protected-resource metadata document of the MCP endpoint at ``endpoint_path``."""
    return f"{_public_base_url()}{PROTECTED_RESOURCE_METADATA_PREFIX}{endpoint_path}"


def _public_base_url() -> str:
    base = access_control_settings().public_base_url
    if base is None:
        raise RuntimeError("ACCESS_CONTROL_PUBLIC_BASE_URL is unset; the MCP door advertises no URL")
    return base


def validate_declared_authorization_servers(providers: Iterable[tuple[str, IdentityProvider]]) -> None:
    """Refuse an invalid declaration and warn about a declaring provider the door cannot advertise.

    Run once per build over the providers it just instantiated. Each declared value must be
    an absolute ``https`` URL (``http`` only on a loopback host) with no query, fragment or
    userinfo, else ``RuntimeError`` naming the provider and the value — a bad declaration
    fails the build, it is never dropped. A provider that declares a server while
    ``ACCESS_CONTROL_PUBLIC_BASE_URL`` is unset logs one WARNING per build.
    """
    declaring: list[str] = []
    for name, provider in providers:
        servers = tuple(provider.authorization_servers())
        for value in servers:
            reason = _invalid_issuer_reason(value)
            if reason is not None:
                raise RuntimeError(
                    f"identity provider {name!r} declares the authorization server {value!r}, which is not "
                    f"a valid issuer identifier: {reason}"
                )
        if servers:
            declaring.append(name)
    if declaring and access_control_settings().public_base_url is None:
        logger.warning(
            "identity provider(s) %s declare an authorization server; MCP client sign-in is not advertised "
            "until ACCESS_CONTROL_PUBLIC_BASE_URL is set",
            ", ".join(repr(name) for name in declaring),
        )


def _invalid_issuer_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return "it is not a string"
    if "?" in value or "#" in value:
        return "it must carry no query and no fragment"
    parts = urlsplit(value)
    if "@" in parts.netloc:
        return "it must carry no userinfo"
    host = parts.hostname
    if not host:
        return "it must be an absolute URL with a host"
    if parts.scheme == "https" or (parts.scheme == "http" and is_loopback_host(host)):
        return None
    return "it must be https:// (http:// only on a loopback host)"
