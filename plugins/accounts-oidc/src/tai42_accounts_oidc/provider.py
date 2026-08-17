"""``OidcAccountsProvider`` — OIDC/OAuth2 login, Redis sessions, dual registration.

Accounts live at the external issuer; this plugin holds no relational data —
sessions, the OAuth ``state`` record, and the one-time SSO code are all TTL'd
key-value records in the injected Redis. Registered at import.

Provider state (the injected settings, the resolved per-provider runtimes) lives on
the per-epoch provider INSTANCE, not a module holder: the routes and the record
helpers resolve the CURRENT epoch's instance through the app's accounts facet
(:func:`provider_settings` / :func:`get_runtime`), so a failed epoch build's provider
is discarded with its core and never leaks into the live epoch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any, cast

import httpx
from tai42_contract.access_control.identity import AuthIdentity
from tai42_contract.accounts import (
    AccountsProvider,
    ButtonMethod,
    LoginMethod,
    register_accounts_provider,
)
from tai42_contract.app import tai42_app
from tai42_kit.clients import RedisConnectionSettings, client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.net.jwt import JwksCache, JwtVerifyError, OidcDiscovery, fetch_discovery
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_accounts_oidc.presets import GITHUB_AUTHORIZE_URL, ResolvedProvider, resolve_provider
from tai42_accounts_oidc.settings import accounts_oidc_settings

if TYPE_CHECKING:
    from tai42_contract.accounts import AccountsProviderSettings

logger = logging.getLogger(__name__)

# Prefix lets validate_token fast-reject non-session tokens without a Redis hit.
SESSION_TOKEN_PREFIX = "tai-sess-"

# Redis key namespaces: authorize->callback state, one-time SSO hand-back, session.
_STATE_KEY_PREFIX = "acc:oidc:state:"
_SSO_KEY_PREFIX = "acc:oidc:sso:"
_SESSION_KEY_PREFIX = "acc:oidc:sess:"

# TTLs (seconds). State: the authorize round trip. SSO: exchanged immediately.
STATE_TTL_SECONDS = 600
SSO_TTL_SECONDS = 30

# A kid that cannot exist, used to force the healthcheck's JWKS fetch; a fetch that
# simply lacks it confirms reachability.
_HEALTHCHECK_PROBE_KID = "__accounts_oidc_healthcheck_probe__"


# -- live provider resolution (per epoch, no module holder) ---------------------
# The provider state (injected settings, resolved runtimes) lives on the epoch's
# provider INSTANCE. Route handlers and the module-level record helpers resolve the
# CURRENT epoch's instance through the app's accounts facet — so a failed epoch build's
# provider is GC'd with the discarded core and never leaks into the live epoch.


def _active_provider() -> OidcAccountsProvider | None:
    """The current epoch's ``OidcAccountsProvider`` instance, or ``None`` when no OIDC
    provider is active (access control disabled / not configured / build mid-flight)."""
    return cast("OidcAccountsProvider | None", tai42_app.accounts.active_provider("accounts-oidc"))


def provider_settings() -> AccountsProviderSettings:
    """The injected settings of the CURRENT epoch's provider instance; RAISE when no
    provider is active (the accounts kind is disabled)."""
    provider = _active_provider()
    if provider is None:
        raise RuntimeError(
            "tai42-accounts-oidc has no active provider: access control is disabled or "
            "'accounts-oidc' is absent from ACCESS_CONTROL_AUTH_PROVIDERS."
        )
    return provider.settings


def provider_settings_populated() -> bool:
    """Whether an OIDC provider is active this epoch — the boot guard's input."""
    return _active_provider() is not None


def _redis_conn() -> RedisConnectionSettings:
    # Bridge the contract's ``Any`` redis to kit's nominal settings type.
    return cast("RedisConnectionSettings", provider_settings().redis)


# -- per-provider runtime (resolved config + cached discovery/JWKS) -------------


class ProviderRuntime:
    """A resolved provider plus its lazily-fetched, cached discovery and JWKS.

    Discovery is fetched once and cached for the process lifetime; the
    :class:`JwksCache` handles issuer key rotation. GitHub runtimes have no
    discovery (plain OAuth2, fixed endpoints).
    """

    def __init__(self, config: ResolvedProvider) -> None:
        self.config = config
        self._discovery: OidcDiscovery | None = None
        self._jwks: JwksCache | None = None
        self._lock = asyncio.Lock()

    async def discovery(self) -> OidcDiscovery:
        if self.config.issuer is None:  # pragma: no cover - github never calls this
            raise RuntimeError(f"provider {self.config.name!r} is a plain-OAuth2 provider with no discovery")
        if self._discovery is None:
            async with self._lock:
                if self._discovery is None:
                    self._discovery = await fetch_discovery(self.config.issuer)
        return self._discovery

    async def jwks(self) -> JwksCache:
        discovery = await self.discovery()
        if self._jwks is None:
            self._jwks = JwksCache(discovery.jwks_uri)
        return self._jwks


def _build_runtimes() -> dict[str, ProviderRuntime]:
    return {config.name: ProviderRuntime(resolve_provider(config)) for config in accounts_oidc_settings().providers}


def get_runtime(name: str) -> ProviderRuntime | None:
    """The runtime for a configured provider name on the CURRENT epoch's provider
    instance, or ``None`` if no provider is active or the name is unknown."""
    provider = _active_provider()
    if provider is None:
        return None
    return provider._runtimes.get(name)


# -- Redis records: state, SSO code, session ------------------------------------


async def store_state(state_id: str, record: dict[str, Any]) -> None:
    """Write the single-use state record (PKCE verifier, provider, nonce)."""
    async with client_ctx(RedisClient, _redis_conn()) as r:
        await r.set(f"{_STATE_KEY_PREFIX}{state_id}", json.dumps(record), ex=STATE_TTL_SECONDS)


async def take_state(state_id: str) -> dict[str, Any] | None:
    """Atomically read-and-delete the state record (single-use ``GETDEL``)."""
    async with client_ctx(RedisClient, _redis_conn()) as r:
        raw = await r.getdel(f"{_STATE_KEY_PREFIX}{state_id}")
    return None if raw is None else json.loads(raw)


async def store_sso_code(code: str, token: str, user_id: str) -> None:
    """Map a one-time SSO hand-back code to the minted session (short TTL)."""
    async with client_ctx(RedisClient, _redis_conn()) as r:
        await r.set(f"{_SSO_KEY_PREFIX}{code}", json.dumps({"token": token, "user_id": user_id}), ex=SSO_TTL_SECONDS)


async def take_sso_code(code: str) -> dict[str, Any] | None:
    """Atomically read-and-delete the SSO code (single-use ``GETDEL``)."""
    async with client_ctx(RedisClient, _redis_conn()) as r:
        raw = await r.getdel(f"{_SSO_KEY_PREFIX}{code}")
    return None if raw is None else json.loads(raw)


def _session_key(token: str) -> str:
    # SHA-256 at rest — the same primitive api keys use, uniform across types.
    return f"{_SESSION_KEY_PREFIX}{hash_api_key(token)}"


async def mint_session(user_id: str) -> str:
    """Create a session for ``user_id`` and return the RAW token (shown once).

    The stored record holds only plugin-minted fields, never the issuer's JWT
    claims. Redis TTL is the idle window; the absolute deadline is an independent
    hard cap enforced in ``validate_token``.
    """
    settings = accounts_oidc_settings()
    raw = f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    now = int(time.time())
    record = {
        "user_id": user_id,
        "created_at": now,
        "absolute_deadline": now + settings.session_absolute_seconds,
    }
    async with client_ctx(RedisClient, _redis_conn()) as r:
        await r.set(_session_key(raw), json.dumps(record), ex=settings.session_idle_seconds)
    return raw


class OidcAccountsProvider(AccountsProvider):
    """Validate sessions, declare login buttons, and drive the OIDC login flow."""

    def __init__(self, settings: AccountsProviderSettings) -> None:
        # Fail loudly on the two REQUIRED env settings, naming the missing var.
        oidc = accounts_oidc_settings()
        if oidc.state_key is None:
            raise ValueError("TAI_ACCOUNTS_OIDC_STATE_KEY is required (HMAC key for the OAuth state parameter)")
        if oidc.public_base_url is None:
            raise ValueError("TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL is required (the public origin login flows return to)")

        self.settings = settings
        # Per-provider runtimes resolved from this epoch's env config, on the INSTANCE:
        # a failed build's provider (and its runtimes) is discarded with the epoch core,
        # never left in a module holder the live epoch would read.
        self._runtimes = _build_runtimes()

    async def validate_token(self, token: str) -> AuthIdentity | None:
        # Fast-reject non-session tokens without a Redis hit so resolution moves on.
        if not token.startswith(SESSION_TOKEN_PREFIX):
            return None

        key = _session_key(token)
        try:
            async with client_ctx(RedisClient, _redis_conn()) as r:
                raw = await r.get(key)
                if raw is None:
                    # Unknown or idle-expired — indistinguishable from a random token.
                    return None
                record = json.loads(raw)
                now = int(time.time())
                if now > record["absolute_deadline"]:
                    # A matched session past its hard cap: log the refusal and delete it.
                    logger.info("accounts-oidc: session for %s refused (absolute-expired); deleting", record["user_id"])
                    await r.delete(key)
                    return None
                # Slide the idle TTL; the absolute deadline is never extended.
                await r.pexpire(key, accounts_oidc_settings().session_idle_seconds * 1000)
        except Exception as exc:
            # Fail closed: RAISE, never return None (which reads as invalid-credential).
            logger.error("accounts-oidc: session validate failed: %s", exc)
            raise

        return AuthIdentity(user_id=record["user_id"], claims=record)

    def login_methods(self) -> list[LoginMethod]:
        # Static config-derived metadata: one button per configured provider. The
        # authorize href resolves through the login router's mount base (captured at
        # its import) so an operator remap of the route base moves the button too.
        from tai42_accounts_oidc.routes import authorize_path

        return [
            ButtonMethod(
                id=runtime.config.name,
                label=runtime.config.label,
                icon=runtime.config.icon,
                href=authorize_path(runtime.config.name),
            )
            for runtime in self._runtimes.values()
        ]

    async def needs_bootstrap(self) -> bool:
        # Accounts live at the issuer — no first-owner concept here.
        return False

    async def revoke_session(self, token: str) -> bool:
        if not token.startswith(SESSION_TOKEN_PREFIX):
            # Not ours — the logout dispatcher moves on.
            return False
        async with client_ctx(RedisClient, _redis_conn()) as r:
            deleted = await r.delete(_session_key(token))
        return deleted > 0

    async def healthcheck(self) -> None:
        # Runs once at boot: fetch each provider's discovery + JWKS (GitHub: HEAD the
        # authorize endpoint); any failure raises so a broken issuer fails the boot.
        for runtime in self._runtimes.values():
            if runtime.config.kind == "github":
                await self._probe_github()
                continue
            cache = await runtime.jwks()
            # A fetch lacking only the probe kid confirms reachability; a
            # transport/JwksFetchError (not suppressed here) fails the boot.
            with contextlib.suppress(JwtVerifyError):
                await cache.get_key(_HEALTHCHECK_PROBE_KID, "RS256")

    async def _probe_github(self) -> None:
        async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(5.0)) as client:
            response = await client.head(GITHUB_AUTHORIZE_URL)
        if response.status_code >= 500:
            raise RuntimeError(f"GitHub authorize endpoint unhealthy: status {response.status_code}")


# One call registers the factory in both the accounts and identity registries
# under the same name — an accounts provider answers its own session tokens.
register_accounts_provider("accounts-oidc", OidcAccountsProvider)
