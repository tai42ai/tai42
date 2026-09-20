"""The setup door — the one-shot, token-gated deployment initialize.

Covers the operation (creates the owner principal, applies the admin role, mints the
owner's first key owned by the owner, attaches a login when a login-attaching provider is
configured; refuses with a 409 once any principal exists; serializes concurrent setups so
exactly one initializes; refuses a wrong/absent token with a 403 before revealing the
initialized state; backs off a wrong-token flood per IP; refuses with a 501 when the gate
is off or no mint provider is configured; ``TAI_SETUP_OPEN`` skips the gate; compensates a
failure past the owner-create) and the gate lifecycle.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM, registry
from tai42_contract.access_control.identity import ApiKeyIdentityProvider, AuthIdentity, IdentityProvider
from tai42_contract.accounts import LoginAttachingProvider
from tai42_contract.accounts.errors import LoginAttachError, LoginConflictError
from tai42_contract.accounts.models import LoginAttachment, LoginCredential, PasswordCredential

from tai42_skeleton.access_control import management
from tai42_skeleton.access_control import roles as roles_module
from tai42_skeleton.access_control import setup_gate as setup_gate_mod
from tai42_skeleton.access_control.settings import access_control_settings, setup_settings
from tai42_skeleton.operations import BadRequestError, ConflictError, ForbiddenError, NotSupportedError
from tai42_skeleton.operations import setup as setup_ops
from tai42_skeleton.operations.setup import setup_deployment

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx

_TOKEN = "setup-token-xyz"


class _SpyProvider(ApiKeyIdentityProvider):
    """In-memory api-key identity provider modelling the record store the real
    ``tai42-identity-redis`` plugin owns, so the door can be driven without a plugin."""

    def __init__(self) -> None:
        self.identities: dict[str, str] = {}
        self.provision_owners: dict[str, str] = {}

    async def validate_token(self, token: str):  # pragma: no cover - unused here
        return None

    async def provision(self, user_id: str, description: str, *, owner_user_id: str) -> str:
        self.identities[user_id] = description
        self.provision_owners[user_id] = owner_user_id
        return f"sk-{user_id}"

    async def revoke(self, user_id: str) -> bool:
        return self.identities.pop(user_id, None) is not None

    async def update_description(self, user_id: str, description: str) -> bool:  # pragma: no cover - unused here
        if user_id not in self.identities:
            return False
        self.identities[user_id] = description
        return True

    async def list_identities(self) -> list[tuple[str, str]]:
        return list(self.identities.items())


class _ValidatorProvider(IdentityProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:  # pragma: no cover - unused here
        return None


class _FakeLoginAttaching(LoginAttachingProvider):
    def __init__(self, attachment: LoginAttachment) -> None:
        self._attachment = attachment
        self.calls: list[tuple[str, LoginCredential]] = []

    def login_methods(self):  # pragma: no cover - unused here
        return []

    async def validate_token(self, token: str) -> AuthIdentity | None:  # pragma: no cover - unused here
        return None

    async def revoke_session(self, token: str) -> bool:  # pragma: no cover - unused here
        return False

    async def has_login(self, user_id: str) -> bool:  # pragma: no cover - unused here
        return False

    async def attach_login(self, user_id: str, *, credential: LoginCredential) -> LoginAttachment:
        self.calls.append((user_id, credential))
        return self._attachment


class _RaisingLoginAttaching(LoginAttachingProvider):
    """A login-attaching provider whose ``attach_login`` raises a contract attach error,
    modelling a too-short password (``LoginAttachError``) or a login/email collision
    (``LoginConflictError``)."""

    def __init__(self, error: LoginAttachError) -> None:
        self._error = error

    def login_methods(self):  # pragma: no cover - unused here
        return []

    async def validate_token(self, token: str) -> AuthIdentity | None:  # pragma: no cover - unused here
        return None

    async def revoke_session(self, token: str) -> bool:  # pragma: no cover - unused here
        return False

    async def has_login(self, user_id: str) -> bool:  # pragma: no cover - unused here
        return False

    async def attach_login(self, user_id: str, *, credential: LoginCredential) -> LoginAttachment:
        raise self._error


@pytest.fixture
def provider() -> _SpyProvider:
    spy = _SpyProvider()
    registry._REGISTRY["redis"] = lambda _settings: spy
    return spy


@pytest.fixture
def setup_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """The AC Redis the gate's throttle counters and mint mutex live on."""
    fake = FakeRedis(strings={})
    monkeypatch.setattr(setup_gate_mod, "client_ctx", make_client_ctx(fake))
    monkeypatch.setattr(management, "client_ctx", make_client_ctx(fake))
    return fake


@pytest.fixture
def operator_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin an operator-set setup token, gate on, so the token resolves without a Redis auto-token."""
    monkeypatch.setattr(setup_settings(), "token", SecretStr(_TOKEN))
    monkeypatch.setattr(setup_settings(), "open", False)
    monkeypatch.setattr(access_control_settings(), "enable", True)


@pytest.fixture
def role_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record ``apply_role`` calls without the versioned role store."""
    calls: list[tuple[str, str]] = []

    async def _apply(user_id: str, role: str) -> None:
        calls.append((user_id, role))

    monkeypatch.setattr(roles_module, "apply_role", _apply)
    return calls


@pytest.fixture(autouse=True)
def _no_login_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: no login-attaching provider is active (a keys-only deployment)."""
    monkeypatch.setattr(setup_ops, "_active_login_attaching_provider", lambda: None)


async def test_setup_creates_owner_principal_key_and_role(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None, role_spy
) -> None:
    result = await setup_deployment(
        setup_token=_TOKEN, owner_user_id="owner", owner_display_name="Owner", key_user_id="owner-key"
    )
    assert result["owner_user_id"] == "owner"
    assert result["key_user_id"] == "owner-key"
    assert result["api_key"] == "sk-owner-key"
    assert result["key_fingerprint"]
    assert result["login_attached"] is False
    # The owner principal is a human, created by the setup door (created_by NULL).
    owner = pg.principal("owner")
    assert owner["kind"] == "human"
    assert owner["display_name"] == "Owner"
    assert owner["created_by"] is None
    # The admin role was applied to the owner.
    assert role_spy == [("owner", roles_module.RESERVED_ADMIN_ROLE)]
    # The owner's key is minted owned by the owner (both homes).
    assert provider.provision_owners["owner-key"] == "owner"
    assert pg.policy_body("owner-key")["policy_data"][OWNER_USER_ID_CLAIM] == "owner"
    assert pg.policy_body("owner-key")["policy_data"][KEY_FINGERPRINT_CLAIM] == result["key_fingerprint"]


async def test_setup_mints_ids_when_absent(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None, role_spy
) -> None:
    result = await setup_deployment(setup_token=_TOKEN, owner_display_name="Owner")
    assert result["owner_user_id"].startswith("usr-")
    assert result["key_user_id"] == f"{result['owner_user_id']}-key"
    assert pg.principal(result["owner_user_id"]) is not None


async def test_setup_409_once_a_principal_exists(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None, role_spy
) -> None:
    pg.add_principal("someone", kind="human", display_name="Someone")
    with pytest.raises(ConflictError, match="Already initialized"):
        await setup_deployment(setup_token=_TOKEN, owner_user_id="owner", owner_display_name="Owner")
    # Nothing minted for the refused owner.
    assert provider.identities == {}
    assert pg.principal("owner") is None


async def test_setup_wrong_token_is_403(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None, role_spy
) -> None:
    with pytest.raises(ForbiddenError, match="Forbidden"):
        await setup_deployment(setup_token="nope", owner_user_id="owner", owner_display_name="Owner")
    assert provider.identities == {}
    assert pg.principal("owner") is None


async def test_setup_absent_token_is_403(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None, role_spy
) -> None:
    with pytest.raises(ForbiddenError, match="Forbidden"):
        await setup_deployment(setup_token="", owner_user_id="owner", owner_display_name="Owner")
    assert provider.identities == {}


async def test_setup_wrong_token_flood_backs_off_per_ip(
    pg: FakeAccessControlPg,
    provider: _SpyProvider,
    setup_redis: FakeRedis,
    operator_token: None,
    role_spy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access_control_settings(), "setup_throttle_threshold", 2)
    ip = "203.0.113.7"
    for _ in range(3):
        with pytest.raises(ForbiddenError):
            await setup_deployment(setup_token="wrong", owner_display_name="Owner", client_ip=ip)
    assert await setup_gate_mod.setup_throttle_locked(ip) is True
    # While locked, even the CORRECT token is refused WITHOUT minting (no oracle, no bypass).
    with pytest.raises(ForbiddenError):
        await setup_deployment(setup_token=_TOKEN, owner_display_name="Owner", client_ip=ip)
    assert provider.identities == {}
    # A different IP is unaffected and can still initialize.
    result = await setup_deployment(setup_token=_TOKEN, owner_display_name="Owner", client_ip="198.51.100.9")
    assert result["api_key"].startswith("sk-")


async def test_setup_open_bypasses_the_token(
    pg: FakeAccessControlPg,
    provider: _SpyProvider,
    setup_redis: FakeRedis,
    role_spy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access_control_settings(), "enable", True)
    monkeypatch.setattr(setup_settings(), "open", True)
    result = await setup_deployment(setup_token="anything", owner_user_id="owner", owner_display_name="Owner")
    assert result["owner_user_id"] == "owner"


async def test_setup_refused_when_gate_is_off(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(access_control_settings(), "enable", False)
    monkeypatch.setattr(setup_settings(), "token", SecretStr(_TOKEN))
    with pytest.raises(NotSupportedError, match="access-controlled installs only"):
        await setup_deployment(setup_token=_TOKEN, owner_display_name="Owner")
    assert provider.identities == {}


async def test_setup_refused_without_a_mint_provider(
    pg: FakeAccessControlPg, setup_redis: FakeRedis, operator_token: None, role_spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_kit.settings import reset_all_settings

    registry._REGISTRY["validator"] = lambda _s: _ValidatorProvider()
    monkeypatch.setenv("ACCESS_CONTROL_AUTH_PROVIDERS", '["validator"]')
    reset_all_settings()
    monkeypatch.setattr(access_control_settings(), "enable", True)
    monkeypatch.setattr(setup_settings(), "token", SecretStr(_TOKEN))
    try:
        with pytest.raises(NotSupportedError, match="no configured identity provider"):
            await setup_deployment(setup_token=_TOKEN, owner_display_name="Owner")
    finally:
        registry._REGISTRY.pop("validator", None)
        reset_all_settings()


async def test_setup_attaches_a_login_when_a_provider_is_present(
    pg: FakeAccessControlPg,
    provider: _SpyProvider,
    setup_redis: FakeRedis,
    operator_token: None,
    role_spy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attaching = _FakeLoginAttaching(
        LoginAttachment(attached=True, invite_token="inv-1", login_path="/login#invite=inv-1")
    )
    monkeypatch.setattr(setup_ops, "_active_login_attaching_provider", lambda: attaching)
    cred = PasswordCredential(email="owner@example.com", password="s3cret")
    result = await setup_deployment(setup_token=_TOKEN, owner_user_id="owner", owner_display_name="Owner", login=cred)
    assert result["login_attached"] is True
    assert result["invite_token"] == "inv-1"
    assert result["login_path"] == "/login#invite=inv-1"
    assert attaching.calls == [("owner", cred)]


async def test_setup_bad_login_credential_is_400_and_compensated(
    pg: FakeAccessControlPg,
    provider: _SpyProvider,
    setup_redis: FakeRedis,
    operator_token: None,
    role_spy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attaching = _RaisingLoginAttaching(LoginAttachError("password must be at least 8 characters"))
    monkeypatch.setattr(setup_ops, "_active_login_attaching_provider", lambda: attaching)
    cred = PasswordCredential(email="owner@example.com", password="short")
    with pytest.raises(BadRequestError, match="password must be at least"):
        await setup_deployment(setup_token=_TOKEN, owner_user_id="owner", owner_display_name="Owner", login=cred)
    # Compensated so setup stays retriable: no principal, no policy, no key.
    assert pg.principal("owner") is None
    assert pg.policy("owner") is None
    assert provider.identities == {}


async def test_setup_login_conflict_is_409_and_compensated(
    pg: FakeAccessControlPg,
    provider: _SpyProvider,
    setup_redis: FakeRedis,
    operator_token: None,
    role_spy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attaching = _RaisingLoginAttaching(LoginConflictError("email already registered: 'owner@example.com'"))
    monkeypatch.setattr(setup_ops, "_active_login_attaching_provider", lambda: attaching)
    cred = PasswordCredential(email="owner@example.com", password="s3cret-enough")
    with pytest.raises(ConflictError, match="already registered"):
        await setup_deployment(setup_token=_TOKEN, owner_user_id="owner", owner_display_name="Owner", login=cred)
    assert pg.principal("owner") is None
    assert pg.policy("owner") is None
    assert provider.identities == {}


async def test_setup_compensates_a_failure_past_the_owner_create(
    pg: FakeAccessControlPg,
    provider: _SpyProvider,
    setup_redis: FakeRedis,
    operator_token: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(user_id: str, role: str) -> None:
        raise RuntimeError("role apply failed")

    monkeypatch.setattr(roles_module, "apply_role", _boom)
    with pytest.raises(RuntimeError, match="role apply failed"):
        await setup_deployment(setup_token=_TOKEN, owner_user_id="owner", owner_display_name="Owner")
    # The owner principal is torn down, so setup stays retriable.
    assert pg.principal("owner") is None
    assert pg.policy("owner") is None


async def test_setup_lock_is_mutually_exclusive(setup_redis: FakeRedis) -> None:
    async with setup_gate_mod.setup_lock():
        with pytest.raises(setup_gate_mod.SetupContendedError):
            async with setup_gate_mod.setup_lock():
                pass  # pragma: no cover - the acquire raises before the body
    async with setup_gate_mod.setup_lock():
        pass


async def test_contended_setup_is_refused_without_initializing(
    pg: FakeAccessControlPg, provider: _SpyProvider, setup_redis: FakeRedis, operator_token: None, role_spy
) -> None:
    async with setup_gate_mod.setup_lock():
        with pytest.raises(ConflictError, match="Already initialized"):
            await setup_deployment(setup_token=_TOKEN, owner_user_id="owner", owner_display_name="Owner")
    assert provider.identities == {}
    assert pg.principal("owner") is None
