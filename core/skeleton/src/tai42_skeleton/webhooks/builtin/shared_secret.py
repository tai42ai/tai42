"""The builtin ``shared_secret`` webhook verifier.

A provider-less core primitive: it checks that a named request header equals a
shared secret, giving any ``universal_webhook`` topic a minimal lock with zero
provider code. Header-based (not a body signature), so it works over any
delivery method — it is NOT ``post_only``.

Config: ``{"header": <header name>, "secret_env": <env var name>, "id_header":
<header name>}``. The secret value is NEVER stored in the config — only the NAME
of the env var holding it, resolved at verify time. A missing env var raises
loudly (fails CLOSED).

The secret is a static bearer token, so it carries no freshness of its own; the
scheme therefore REQUIRES ``id_header`` naming a header the sender fills with a
UNIQUE per-delivery id (a UUID/nonce). That id is the seen-set key the ingress
dedups on, so a captured delivery cannot be replayed. (Capturing a delivery also
reveals the plaintext secret, so a secret-compromise forge is inherent to this
scheme and outside replay defense — bind a signature verifier where that matters.)

Registered under the name ``shared_secret`` on import; load it with a manifest
``lifecycle_modules`` entry: ``tai42_skeleton.webhooks.builtin.shared_secret``.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.webhooks import ReplayDefense, SeenSetClaim, WebhookVerificationError

# How long a delivery id is remembered. The scheme carries no signed timestamp, so
# the seen-set is the ONLY replay defense — a longer window defends longer, bounded
# by the id storage it costs. One day by default; ``replay_window_seconds`` overrides.
_DEFAULT_REPLAY_WINDOW_SECONDS = 86400


def _lookup(headers: Mapping[str, str], name: str) -> str | None:
    """Return ``name``'s value from ``headers`` case-insensitively (HTTP header
    names are case-insensitive; a plain Mapping is not, so scan on lowered keys)."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


class SharedSecretVerifier:
    """Verifies a named header equals a shared secret via a constant-time compare."""

    # Header-based: the secret rides a header, not a body signature, so any
    # delivery method (POST or GET) is fine.
    post_only = False

    async def verify(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> None:
        # A missing/malformed config key is an operator misconfiguration of the
        # BINDING (not a request-level failure), so it raises a plain exception ->
        # HTTP 500 (fails CLOSED), distinct from the 401 a bad request signature
        # gets — mirroring the github verifier's ``config[...]`` KeyError.
        header_name = config.get("header")
        secret_env = config.get("secret_env")
        if not isinstance(header_name, str) or not header_name:
            raise ValueError("shared_secret verifier config requires a non-empty 'header'")
        if not isinstance(secret_env, str) or not secret_env:
            raise ValueError("shared_secret verifier config requires a non-empty 'secret_env'")

        # A missing env var is an operator misconfiguration, not a request-level
        # failure: raise loudly (KeyError -> HTTP 500) so the door fails CLOSED
        # rather than silently treating an unresolved secret as a match/mismatch.
        secret = os.environ[secret_env]

        provided = _lookup(headers, header_name)
        if provided is None:
            raise WebhookVerificationError("shared_secret verification failed")

        # compare_digest is constant-time even for a plain token compare, so a
        # timing side channel can't leak the secret one byte at a time. Compare on
        # UTF-8 bytes: a header value can carry non-ASCII (Starlette decodes headers
        # as latin-1), and compare_digest raises TypeError on non-ASCII ``str`` —
        # encoding first turns that into an ordinary mismatch (401), not a 500.
        if not hmac.compare_digest(provided.encode("utf-8"), secret.encode("utf-8")):
            raise WebhookVerificationError("shared_secret verification failed")

    def replay_defense(self, body: bytes, headers: Mapping[str, str], config: dict[str, Any]) -> ReplayDefense:
        """Defend replay by a seen-set on the sender-supplied ``id_header`` value.

        The static bearer secret carries no freshness, so the scheme requires a unique
        per-delivery id. A missing/malformed ``id_header`` config is an operator
        misconfiguration of the binding (``ValueError`` -> HTTP 500). A delivery whose
        secret verifies but that carries no id under that header cannot be deduped, so it
        is refused (``WebhookVerificationError`` -> 401) rather than dispatched undefended.
        """
        id_header = config.get("id_header")
        if not isinstance(id_header, str) or not id_header:
            raise ValueError("shared_secret verifier config requires a non-empty 'id_header'")
        window = config.get("replay_window_seconds", _DEFAULT_REPLAY_WINDOW_SECONDS)
        if isinstance(window, bool) or not isinstance(window, int):
            raise ValueError(f"replay_window_seconds must be an int, got {window!r}")
        if window <= 0:
            raise ValueError(f"replay_window_seconds must be positive, got {window!r}")
        delivery_id = _lookup(headers, id_header)
        if not delivery_id:
            raise WebhookVerificationError("shared_secret replay id header missing")
        return SeenSetClaim(key=f"shared_secret:{id_header.lower()}:{delivery_id}", ttl_seconds=window)


tai42_app.webhook_verifiers.register("shared_secret", SharedSecretVerifier())
