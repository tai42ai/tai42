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
    # The owner principal every minted/re-minted key in this suite belongs to.
    fake.add_principal("owner-1", kind="human", display_name="Owner One")
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


def _token(user_id: str, description: str = "restored", owner: str = "owner-1") -> dict:
    # Every api key belongs to a principal, so an exported token carries its owner claim.
    return {
        "user_id": user_id,
        "description": description,
        "scopes": [],
        "policy_data": {OWNER_USER_ID_CLAIM: owner},
        "condition": None,
    }


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
    await management.add_user_api_key("live", "live-desc", [], owner_user_id="owner-1")
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


# -- principals travel with the section --------------------------------------


async def test_export_carries_principals_with_their_policy(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    pg.add_policy("owner-1", scopes=["*"])
    doc = await sections._export_access_control()
    assert len(doc["principals"]) == 1
    exported = doc["principals"][0]
    assert exported["user_id"] == "owner-1"
    assert exported["kind"] == "human"
    assert exported["display_name"] == "Owner One"
    assert exported["created_by"] is None
    assert exported["disabled"] is False
    assert exported["policy"]["scopes"] == ["*"]


async def test_import_restores_principals_before_tokens(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    # A fresh principal (owner-2) and a key owned by it: the principal is created FIRST so
    # the token's owner-required mint finds it provisioned.
    payload = {
        "principals": [
            {
                "user_id": "owner-2",
                "kind": "service",
                "display_name": "Owner Two",
                "created_by": "owner-1",
                "disabled": False,
                "created_at": None,
                "policy": {"scopes": ["*"], "policy_data": {}, "condition": None},
            }
        ],
        "tokens": [_token("k1", owner="owner-2")],
    }
    report = await sections._import_access_control(payload)
    created = pg.principal("owner-2")
    assert created is not None
    assert created["kind"] == "service"
    assert created["created_by"] == "owner-1"
    # The token minted, owned by the just-restored principal.
    assert provider.identities["k1"] == "restored"
    assert any(row["user_id"] == "k1" for row in report["new_api_keys"])


async def test_import_existing_principal_is_a_clean_skip(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    # owner-1 already exists (seeded): a restore never overwrites the identity.
    payload = {
        "principals": [
            {
                "user_id": "owner-1",
                "kind": "human",
                "display_name": "Renamed",
                "created_by": None,
                "disabled": False,
                "created_at": None,
                "policy": {"scopes": ["*"], "policy_data": {}, "condition": None},
            }
        ],
        "tokens": [],
    }
    report = await sections._import_access_control(payload)
    assert report["skipped_existing"] == 1
    assert pg.principal("owner-1")["display_name"] == "Owner One"  # untouched


async def test_import_principal_keeps_a_surviving_policy_row(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    # A Redis-only loss recovered from Postgres: the principal row is gone but its policy
    # row survives. The restore creates the principal row and KEEPS the live policy (the
    # Postgres rows are the source of truth), never raising on the existing policy.
    pg.add_policy("owner-3", scopes=["read"], policy_data={"kept": True})
    payload = {
        "principals": [
            {
                "user_id": "owner-3",
                "kind": "service",
                "display_name": "Owner Three",
                "created_by": "owner-1",
                "disabled": False,
                "created_at": None,
                "policy": {"scopes": ["*"], "policy_data": {"exported": True}, "condition": None},
            }
        ],
        "tokens": [],
    }
    report = await sections._import_access_control(payload)
    assert report["created"] == 1
    assert report["errors"] == []
    created = pg.principal("owner-3")
    assert created is not None
    assert created["kind"] == "service"
    # The live policy row is untouched — the exported body did NOT overwrite it.
    assert pg.policy_body("owner-3")["scopes"] == ["read"]
    assert pg.policy_body("owner-3")["policy_data"] == {"kept": True}


async def test_import_ownerless_token_is_a_loud_per_token_error(
    pg: FakeAccessControlPg, provider: _SpyProvider
) -> None:
    # A minted token with no owner claim is an ownerless key row: a loud per-token error,
    # never re-minted ownerless.
    payload = {"tokens": [{"user_id": "k1", "description": "d", "scopes": [], "policy_data": {}, "condition": None}]}
    report = await sections._import_access_control(payload)
    assert report["created"] == 0
    assert report["skipped"] == 1
    assert any("no owner claim" in err for err in report["errors"])
    assert "k1" not in provider.identities
