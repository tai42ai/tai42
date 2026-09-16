"""The ``access_control`` backup section's token restore across the four key states.

An imported token is matched to the live state of its ``user_id``: a live key or a
role-assigned account row is left in place (``skipped_existing``); a user id with no
policy row is minted fresh; an orphaned policy row (its identity record gone after a
partial restore) is re-minted onto the surviving policy, which stays byte-equal.
"""

from __future__ import annotations

import pytest
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM, registry
from tai42_contract.access_control.identity import ApiKeyIdentityProvider

from tai42_skeleton.access_control import management
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.backup import sections

from ..access_control.conftest import FakeAccessControlPg, make_pg_ctx
from ..access_control.conftest import FakeRedis as FakeAccessControlRedis
from ..access_control.conftest import make_client_ctx as make_access_control_client_ctx


class _SpyProvider(ApiKeyIdentityProvider):
    """In-memory api-key identity provider modelling the record store the real
    ``tai42-identity-redis`` plugin owns, so the section can be driven without a plugin."""

    def __init__(self) -> None:
        self.identities: dict[str, str] = {}
        self.provision_owners: dict[str, str | None] = {}

    async def validate_token(self, token: str):  # pragma: no cover - unused here
        return None

    async def provision(self, user_id: str, description: str, *, owner_user_id: str | None = None) -> str:
        self.provision_owners[user_id] = owner_user_id
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


@pytest.fixture(autouse=True)
def _isolate_identity_registry():
    saved = dict(registry._REGISTRY)
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved)


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch) -> FakeAccessControlPg:
    """The policy store over a fake Postgres, wired through the store module's seam."""
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    fake = FakeAccessControlPg()
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(fake))
    return fake


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _SpyProvider:
    """Register a spy as the default ``"redis"`` provider and wire the AC Redis the
    context delete + version bump touch."""
    spy = _SpyProvider()
    registry._REGISTRY["redis"] = lambda _settings: spy
    monkeypatch.setattr(management, "client_ctx", make_access_control_client_ctx(FakeAccessControlRedis()))
    return spy


def _token(user_id: str, description: str = "restored") -> dict:
    return {"user_id": user_id, "description": description, "scopes": [], "policy_data": None, "condition": None}


async def test_import_re_mints_an_orphan_and_leaves_its_policy_unchanged(
    pg: FakeAccessControlPg, provider: _SpyProvider
) -> None:
    owner = "owner-1"
    policy_data = {KEY_FINGERPRINT_CLAIM: "fp-1", OWNER_USER_ID_CLAIM: owner}
    body_before = {"scopes": ["s"], "policy_data": policy_data, "condition": None}
    pg.add_policy("orphan", scopes=["s"], policy_data=dict(policy_data))

    report = await sections._import_access_control({"tokens": [_token("orphan")]})

    # The orphan is re-minted: a fresh identity carrying the re-homed owner claim, the raw
    # key surfaced once, and the surviving policy row byte-equal.
    assert report["created"] == 1
    assert report["skipped_existing"] == 0
    assert report["new_api_keys"] == [{"user_id": "orphan", "description": "restored", "api_key": "sk-orphan"}]
    assert provider.identities["orphan"] == "restored"
    assert provider.provision_owners["orphan"] == owner
    assert pg.policy_body("orphan") == body_before


async def test_import_skips_a_live_key(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    await management.add_user_api_key("live", "live-desc", [])
    report = await sections._import_access_control({"tokens": [_token("live")]})
    # Policy AND identity present: left in place, never re-minted.
    assert report["skipped_existing"] == 1
    assert report["created"] == 0
    assert report["new_api_keys"] == []
    assert provider.identities["live"] == "live-desc"


async def test_import_skips_an_account_row_without_minting(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    pg.add_policy("account", scopes=["hooks"])
    report = await sections._import_access_control({"tokens": [_token("account")]})
    # A role-assigned account row (no fingerprint, no identity) is never overwritten with a key.
    assert report["skipped_existing"] == 1
    assert report["created"] == 0
    assert "account" not in provider.identities
    assert pg.policy_body("account")["scopes"] == ["hooks"]


async def test_import_mints_an_absent_token(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    report = await sections._import_access_control({"tokens": [_token("fresh")]})
    # No policy row for this user id: a brand-new key is minted.
    assert report["created"] == 1
    assert report["skipped_existing"] == 0
    assert report["new_api_keys"] == [{"user_id": "fresh", "description": "restored", "api_key": "sk-fresh"}]
    assert provider.identities["fresh"] == "restored"
