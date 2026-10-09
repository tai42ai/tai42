"""The platform's client-identity document: an ``https`` URL that is its OAuth client id.

An authorization server that supports client-identity documents fetches the URL a flow presents as
its ``client_id`` and reads the client's metadata there. The platform serves one per ``https``
origin of the redirect allow-list other than the central bridge's: a client with no secret never
signs in through the bridge, which forwards a code to whichever origin the unverified ``state``
names. The document lists exactly that origin's own callback.

Pure helpers: settings are read at call time.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from tai42_skeleton.connectors.oauth.redirect import callback_uri
from tai42_skeleton.connectors.settings import connector_engine_config

DOCUMENT_PATH = "/oauth/client-metadata.json"

_HTTPS_DEFAULT_PORT = 443


def document_origins() -> list[str]:
    """The origins the document is served for: the allow-list's ``https`` entries other than the bridge's.

    In the operator's order, each as written in the allow-list.
    """
    config = connector_engine_config()
    bridge = urlsplit(config.oauth_bridge_url.strip()) if config.oauth_bridge_url else None
    bridge_key = _host_key(bridge.netloc) if bridge is not None and bridge.scheme.lower() == "https" else None
    out: list[str] = []
    for origin in config.redirect_uri_allowlist_origins:
        parts = urlsplit(origin)
        if parts.scheme.lower() != "https":
            continue
        key = _host_key(parts.netloc)
        if key is not None and key != bridge_key:
            out.append(origin)
    return out


def client_document_url(origin: str) -> str:
    """The client id a flow from ``origin`` presents: the URL of the document for that origin."""
    return f"{origin}{DOCUMENT_PATH}"


def eligible(origin: str) -> bool:
    """Whether a flow from ``origin`` may use the document: the setting is on and ``origin`` is a document origin."""
    return connector_engine_config().client_id_document and origin in document_origins()


def is_own_client_document(client_id: str) -> bool:
    """Whether ``client_id`` is the document URL of one of this deployment's document origins, the setting on."""
    if not connector_engine_config().client_id_document:
        return False
    return any(client_id == client_document_url(origin) for origin in document_origins())


def client_document(origin: str) -> dict[str, Any]:
    """The document served for ``origin``."""
    return {
        "client_id": client_document_url(origin),
        "client_name": connector_engine_config().client_name,
        "client_uri": origin,
        "redirect_uris": [callback_uri(origin)],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def _host_key(netloc: str) -> tuple[str, int | None] | None:
    """``(host, port)`` of a ``host[:port]`` authority, the default ``https`` port left out; ``None`` if malformed."""
    try:
        parts = urlsplit(f"//{netloc.strip()}")
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    return host.lower(), None if port == _HTTPS_DEFAULT_PORT else port


def origin_for_host(host_header: str | None) -> str | None:
    """The document origin a request answers for, by its ``Host``; ``None`` when none matches.

    With exactly one document origin it is that one, whatever the ``Host`` (proxies often rewrite
    it). With several, the one whose host and port equal the request's (case-insensitive, the
    default ``https`` port left out on both sides). The scheme always comes from the allow-list.
    """
    origins = document_origins()
    if len(origins) == 1:
        return origins[0]
    wanted = _host_key(host_header) if host_header else None
    if wanted is None:
        return None
    for origin in origins:
        if _host_key(urlsplit(origin).netloc) == wanted:
            return origin
    return None
