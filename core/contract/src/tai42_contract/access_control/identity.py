"""Identity contract: resolve a caller's token into an authenticated identity."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, NamedTuple, Protocol, runtime_checkable


class AuthIdentity(NamedTuple):
    user_id: str
    claims: dict[str, Any]


class ReadinessTarget(NamedTuple):
    """A backing store core's readiness probe should ping on a provider's behalf.

    ``name`` is the readiness-check label the probe reports under; ``client`` is
    the kit client CLASS that opens the connection; ``settings`` is that client's
    connection settings. Core enumerates the active provider's targets, dedupes
    them against every other subsystem's connections by identity, and pings each
    once under the readiness budget — so an identity store shared with another
    subsystem is pinged once and both report that one result.

    ``client`` and ``settings`` are typed loosely (``type`` / ``Any``): the
    contract layer does not depend on tai42-kit, so it cannot name kit's client or
    connection-settings classes. A provider fills them with the concrete kit types.
    """

    name: str
    client: type
    settings: Any


OWNER_USER_ID_CLAIM = "owner_user_id"
"""Identity-record claim naming the account that owns an API key.

Written by ``ApiKeyIdentityProvider.provision`` implementations when an
owner is given; read by the application's per-request attenuation. A record
without this claim is an ownerless machine key.

Authoritative ONLY from the mint path (``ApiKeyIdentityProvider.provision``).
The application's verifier ENFORCES this: it strips this claim from the
resolved claims of any provider that is not an ``ApiKeyIdentityProvider``, so
owner attenuation can never be driven by an external issuer's claims. Provider
implementations SHOULD NOT emit this claim; a non-mintable provider that copies
external claims into the identity it returns MAY strip it itself as
defense-in-depth, but the enforced guarantee is the verifier's central strip.
"""


KEY_FINGERPRINT_CLAIM = "key_fingerprint"
"""``policy_data`` claim holding a key's per-mint identity (fresh ``uuid4`` hex per mint).

Bindings resolve against this, not the reusable ``user_id``, so a revoke+remint of the
same ``user_id`` fails closed. Authorization anchor, never a display field.
"""


class IdentityProvider(ABC):
    """Validates an opaque token and returns the caller's identity."""

    @abstractmethod
    async def validate_token(self, token: str) -> AuthIdentity | None:
        """Return the :class:`AuthIdentity` for a valid token, or ``None`` if the
        token is not valid."""
        ...

    async def healthcheck(self) -> None:
        """Probe the provider's OWN storage; raise loudly on failure.

        Default no-op — a provider whose storage needs no boot probe inherits it.
        The skeleton awaits this at startup when access control is enabled, for
        WHATEVER provider is active (an OIDC/SAML provider that implements only this
        base class is boot-probed too), so a provider whose own backend is unusable
        fails the boot instead of the first authenticated request."""
        return None

    def readiness_targets(self) -> Sequence[ReadinessTarget]:
        """Declare the backing store(s) core's ``/ready`` probe should ping for this
        provider — the mechanism by which core health-checks the active identity
        provider WITHOUT naming a concrete provider.

        Each :class:`ReadinessTarget` carries a check label plus the kit client class
        and connection settings core opens and pings, deduped against every other
        subsystem's connections so a shared store is pinged once. Returning a target
        is behaviourally identical to core having wired that connection itself.

        Default: no targets — a provider with no pingable backing store (e.g. one
        that validates tokens against an external IdP over HTTP) inherits the empty
        tuple, exactly as it inherits the ``healthcheck`` no-op. A provider backed by
        its own store overrides this to declare that store."""
        return ()


class ApiKeyIdentityProvider(IdentityProvider):
    """Provisioning interface for key-MINTING identity providers.

    Implemented only by providers that mint API keys (an OIDC/SAML provider does
    not mint and implements the plain :class:`IdentityProvider`). The api-key
    identity record — key hash -> ``{user_id, description}`` plus the reverse
    user -> hash lookup — is OWNED by the provider's storage. Every method here
    mutates the provider's OWN storage; none touches the skeleton's stores.
    """

    @abstractmethod
    async def provision(self, user_id: str, description: str, *, owner_user_id: str | None = None) -> str:
        """Write the key->identity record in the provider's OWN storage and return
        the RAW key. The identity record — key hash -> ``{user_id, description}``
        plus the reverse user -> hash lookup — is owned by the provider. Async
        because provider storage (e.g. Redis) is reached over an async client.

        MUST reject a duplicate ``user_id`` — one that already has an identity
        record — by raising ``ValueError`` ATOMICALLY against its own storage,
        never by overwriting or appending. Per-user uniqueness of the record is the
        provider's to guarantee: two concurrent provisions of the same ``user_id``
        must not both write a record (which would strand one identity document
        unreachable through the reverse lookup, hence unrevocable). The
        orchestrating layer relies on this atomic guard, so a cross-backend
        pre-check is not a substitute.

        When ``owner_user_id`` is given, the implementation MUST persist it on the
        identity record under :data:`OWNER_USER_ID_CLAIM` so it surfaces in
        ``AuthIdentity.claims`` on every subsequent ``validate_token``. ``None``
        mints an ownerless machine key; whether a caller is ALLOWED to mint
        ownerless keys is application policy, not provider logic."""
        ...

    @abstractmethod
    async def revoke(self, user_id: str) -> bool:
        """Delete the identity record so the key stops authenticating immediately.
        Return ``False`` if the user is unknown."""
        ...

    @abstractmethod
    async def update_description(self, user_id: str, description: str) -> bool:
        """Rewrite the ``description`` field of the stored identity record — its
        single home. Return ``False`` if the user is unknown."""
        ...

    @abstractmethod
    async def list_identities(self) -> list[tuple[str, str]]:
        """Enumerate every stored identity as ``(user_id, description)``."""
        ...


@runtime_checkable
class IdentityProviderSettings(Protocol):
    """The settings shape an identity-provider factory receives.

    A provider factory is ``Callable[[IdentityProviderSettings], IdentityProvider]``.
    The concrete settings class lives in the application layer and structurally
    satisfies this Protocol, so the factory contract stays in the contract layer
    without the contract naming an application class.

    ``redis`` is typed ``Any``: tai42-contract does not depend on tai42-kit, so this
    Protocol cannot name kit's ``RedisConnectionSettings``. A provider that opens
    a kit client from ``redis`` must ``cast`` the value at its ``client_ctx`` call
    site — kit's ``client_ctx`` takes a NOMINAL settings param, which a
    structurally-typed value does not satisfy on its own.
    """

    key_prefix: str
    redis: Any


__all__ = [
    "KEY_FINGERPRINT_CLAIM",
    "OWNER_USER_ID_CLAIM",
    "ApiKeyIdentityProvider",
    "AuthIdentity",
    "IdentityProvider",
    "IdentityProviderSettings",
    "ReadinessTarget",
]
