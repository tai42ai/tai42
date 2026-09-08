"""The first-key bootstrap door — the one-shot, token-gated first-admin mint.

Covers the operation (mints a condition-free ``["*"]`` admin key once; refuses with a
409 once any key exists; serializes concurrent bootstraps so exactly one mints; refuses a
wrong/absent token with a 403 before revealing the initialized state; backs off a
wrong-token flood per IP; refuses with a 501 when the gate is off; ``bootstrap_open`` skips
the gate) and the token lifecycle (``SET NX`` fixes and logs the auto-token exactly once).
"""

from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr
from tai42_contract.access_control import OWNER_USER_ID_CLAIM, registry
from tai42_contract.access_control.identity import ApiKeyIdentityProvider

from tai42_skeleton.access_control import bootstrap as bootstrap_mod
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.operations import ConflictError, ForbiddenError, NotSupportedError
from tai42_skeleton.operations.keys_bootstrap import bootstrap_admin_key

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx

_TOKEN = "boot-token-xyz"


class _SpyProvider(ApiKeyIdentityProvider):
    """In-memory api-key identity provider modelling the record store the real
    ``tai42-identity-redis`` plugin owns, so the door can be driven without a plugin."""

    def __init__(self) -> None:
        self.identities: dict[str, str] = {}

    async def validate_token(self, token: str):  # pragma: no cover - unused here
        return None

    async def provision(self, user_id: str, description: str, *, owner_user_id: str | None = None) -> str:
        self.identities[user_id] = description
        return f"sk-{user_id}"

    async def revoke(self, user_id: str) -> bool:  # pragma: no cover - unused here
        return self.identities.pop(user_id, None) is not None

    async def update_description(self, user_id: str, description: str) -> bool:  # pragma: no cover - unused here
        if user_id not in self.identities:
            return False
        self.identities[user_id] = description
        return True

    async def list_identities(self) -> list[tuple[str, str]]:
        return list(self.identities.items())


@pytest.fixture
def provider() -> _SpyProvider:
    """Register a spy as the default ``"redis"`` provider; the autouse registry-isolation
    fixture restores the real registration afterwards."""
    spy = _SpyProvider()
    registry._REGISTRY["redis"] = lambda _settings: spy
    return spy


@pytest.fixture
def bootstrap_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """The AC Redis the door's throttle counters and mint mutex live on."""
    fake = FakeRedis(strings={})
    monkeypatch.setattr(bootstrap_mod, "client_ctx", make_client_ctx(fake))
    return fake


@pytest.fixture
def operator_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin an operator-set bootstrap token so the gate resolves without a Redis auto-token."""
    monkeypatch.setattr(access_control_settings(), "bootstrap_token", SecretStr(_TOKEN))
    monkeypatch.setattr(access_control_settings(), "bootstrap_open", False)
    monkeypatch.setattr(access_control_settings(), "enable", True)


async def test_bootstrap_mints_the_first_admin_key(
    pg: FakeAccessControlPg, provider: _SpyProvider, bootstrap_redis: FakeRedis, operator_token: None
) -> None:
    result = await bootstrap_admin_key("root", "root key", _TOKEN)

    # The raw sk- key is returned once; the identity record lands in the provider's store.
    assert result == {"token": "sk-root", "user_id": "root"}
    assert provider.identities == {"root": "root key"}

    # The policy is the admin discriminator: a condition-free ["*"] that is not owned.
    policy = pg.policy_body("root")
    assert policy["scopes"] == ["*"]
    assert policy["condition"] is None
    assert policy["condition_id"] is None
    assert OWNER_USER_ID_CLAIM not in (policy["policy_data"] or {})


async def test_bootstrap_refused_once_a_key_exists(
    pg: FakeAccessControlPg, provider: _SpyProvider, bootstrap_redis: FakeRedis, operator_token: None
) -> None:
    await bootstrap_admin_key("root", "root key", _TOKEN)

    with pytest.raises(ConflictError, match="Already initialized"):
        await bootstrap_admin_key("second", "another", _TOKEN)
    # No second identity minted.
    assert list(provider.identities) == ["root"]


async def test_mint_lock_is_mutually_exclusive(bootstrap_redis: FakeRedis) -> None:
    # The mutex the check-and-mint runs under: while one holder is inside, a second
    # acquisition is refused (BootstrapContended) — the serialization that makes the
    # existence-check-and-mint atomic. It is released on exit, so a later mint can proceed.
    async with bootstrap_mod.bootstrap_mint_lock():
        with pytest.raises(bootstrap_mod.BootstrapContended):
            async with bootstrap_mod.bootstrap_mint_lock():
                pass  # pragma: no cover - the acquire raises before the body
    # Freed on exit.
    async with bootstrap_mod.bootstrap_mint_lock():
        pass


async def test_contended_mint_is_refused_without_minting(
    pg: FakeAccessControlPg, provider: _SpyProvider, bootstrap_redis: FakeRedis, operator_token: None
) -> None:
    # A bootstrap that loses the mutex to a concurrent one is turned away 409 and mints
    # nothing — the loser of a real parallel race (proven end-to-end in the e2e leg).
    async with bootstrap_mod.bootstrap_mint_lock():
        with pytest.raises(ConflictError, match="Already initialized"):
            await bootstrap_admin_key("root", "root key", _TOKEN)
    assert provider.identities == {}
    assert pg.policy("root") is None


async def test_bootstrap_refused_on_a_wrong_token(
    pg: FakeAccessControlPg, provider: _SpyProvider, bootstrap_redis: FakeRedis, operator_token: None
) -> None:
    with pytest.raises(ForbiddenError, match="Forbidden"):
        await bootstrap_admin_key("root", "root key", "not-the-token")
    # Nothing is minted, and the door never reveals the initialized state to a bad token.
    assert provider.identities == {}
    assert pg.policy("root") is None


async def test_bootstrap_refused_on_an_absent_token(
    pg: FakeAccessControlPg, provider: _SpyProvider, bootstrap_redis: FakeRedis, operator_token: None
) -> None:
    with pytest.raises(ForbiddenError, match="Forbidden"):
        await bootstrap_admin_key("root", "root key", "")
    assert provider.identities == {}


async def test_wrong_token_flood_backs_off_per_ip(
    pg: FakeAccessControlPg,
    provider: _SpyProvider,
    bootstrap_redis: FakeRedis,
    operator_token: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access_control_settings(), "bootstrap_throttle_threshold", 2)
    ip = "203.0.113.7"

    # Past the threshold the IP is locked out.
    for _ in range(3):
        with pytest.raises(ForbiddenError):
            await bootstrap_admin_key("root", "root key", "wrong", client_ip=ip)
    assert await bootstrap_mod.bootstrap_throttle_locked(ip) is True

    # While locked, even the CORRECT token is refused WITHOUT minting (no oracle, no bypass).
    with pytest.raises(ForbiddenError):
        await bootstrap_admin_key("root", "root key", _TOKEN, client_ip=ip)
    assert provider.identities == {}

    # A different IP is unaffected and can still mint.
    result = await bootstrap_admin_key("root", "root key", _TOKEN, client_ip="198.51.100.9")
    assert result["token"] == "sk-root"


async def test_bootstrap_refused_when_gate_is_off(
    pg: FakeAccessControlPg, provider: _SpyProvider, bootstrap_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(access_control_settings(), "enable", False)
    monkeypatch.setattr(access_control_settings(), "bootstrap_token", SecretStr(_TOKEN))

    with pytest.raises(NotSupportedError, match="access-controlled installs only"):
        await bootstrap_admin_key("root", "root key", _TOKEN)
    assert provider.identities == {}


async def test_bootstrap_open_skips_the_token_gate(
    pg: FakeAccessControlPg, provider: _SpyProvider, bootstrap_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(access_control_settings(), "enable", True)
    monkeypatch.setattr(access_control_settings(), "bootstrap_open", True)
    monkeypatch.setattr(access_control_settings(), "bootstrap_token", None)

    result = await bootstrap_admin_key("root", "root key", "any-value-works")
    assert result == {"token": "sk-root", "user_id": "root"}
    assert provider.identities == {"root": "root key"}


@pytest.fixture
def serviceable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the bootstrap door serviceable: the gate on, a key-minting provider
    (the ``provider`` spy), and the AC Redis URL set — the three conditions the
    startup handler mints an auto-token under."""
    monkeypatch.setattr(access_control_settings(), "enable", True)
    monkeypatch.setattr(access_control_settings(), "bootstrap_token", None)
    monkeypatch.setattr(access_control_settings(), "bootstrap_open", False)
    monkeypatch.setattr(access_control_settings().redis, "redis_url", "redis://fake:6379/0")


async def test_ensure_bootstrap_token_fixes_and_logs_once(
    provider: _SpyProvider,
    serviceable: None,
    bootstrap_redis: FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="tai42_skeleton.access_control.bootstrap"):
        await bootstrap_mod.ensure_bootstrap_token()
        await bootstrap_mod.ensure_bootstrap_token()

    lines = [r for r in caplog.records if "first-key bootstrap token" in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    # The winner's value is the effective token the door then resolves.
    key = access_control_settings().bootstrap_token_key
    assert await bootstrap_mod.resolve_bootstrap_token() == bootstrap_redis._strings[key]


async def test_ensure_bootstrap_token_is_a_noop_without_the_ac_redis(
    provider: _SpyProvider, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A Redis-less deployment (an optional feature it does not use) must boot: the
    startup handler mints nothing and raises nothing when the AC Redis is unset, even
    with the gate on and a minting provider present. Uses the REAL ``client_ctx`` (no
    fake): unfixed code reaches for the unconfigured AC Redis at boot instead of a
    clean no-op."""
    monkeypatch.setattr(access_control_settings(), "enable", True)
    monkeypatch.setattr(access_control_settings(), "bootstrap_token", None)
    monkeypatch.setattr(access_control_settings(), "bootstrap_open", False)
    monkeypatch.setattr(access_control_settings().redis, "redis_url", None)

    with caplog.at_level(logging.INFO, logger="tai42_skeleton.access_control.bootstrap"):
        await bootstrap_mod.ensure_bootstrap_token()

    assert not any("first-key bootstrap token" in r.getMessage() for r in caplog.records)


async def test_ensure_bootstrap_token_is_a_noop_without_a_minting_provider(
    monkeypatch: pytest.MonkeyPatch, bootstrap_redis: FakeRedis, caplog: pytest.LogCaptureFixture
) -> None:
    """The door self-disables (501) when no configured provider can mint api keys, so
    the startup handler mints no auto-token then either — the deployment boots. Mirrors
    the plugins/agents skeleton e2e, whose only provider is a non-api-key stand-in."""
    from tai42_contract.access_control.identity import IdentityProvider

    class _NonMintingProvider(IdentityProvider):
        def __init__(self, _settings: object) -> None: ...

        async def validate_token(self, token: str) -> None:
            return None

    registry._REGISTRY["redis"] = _NonMintingProvider
    monkeypatch.setattr(access_control_settings(), "enable", True)
    monkeypatch.setattr(access_control_settings(), "bootstrap_token", None)
    monkeypatch.setattr(access_control_settings(), "bootstrap_open", False)
    monkeypatch.setattr(access_control_settings().redis, "redis_url", "redis://fake:6379/0")

    with caplog.at_level(logging.INFO, logger="tai42_skeleton.access_control.bootstrap"):
        await bootstrap_mod.ensure_bootstrap_token()

    assert not any("first-key bootstrap token" in r.getMessage() for r in caplog.records)
    key = access_control_settings().bootstrap_token_key
    assert key not in bootstrap_redis._strings


async def test_operator_token_takes_precedence_over_the_auto_token(
    bootstrap_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(access_control_settings(), "bootstrap_token", SecretStr(_TOKEN))

    with caplog.at_level(logging.INFO, logger="tai42_skeleton.access_control.bootstrap"):
        await bootstrap_mod.ensure_bootstrap_token()

    # An operator token means no auto-token is generated or logged, and it resolves directly.
    assert not any("first-key bootstrap token" in r.getMessage() for r in caplog.records)
    assert await bootstrap_mod.resolve_bootstrap_token() == _TOKEN
    assert await bootstrap_mod.verify_bootstrap_token(_TOKEN) is True
    assert await bootstrap_mod.verify_bootstrap_token("nope") is False
