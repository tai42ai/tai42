"""The ``access_control`` backup section: export shape, principals restore, and token restore.

An imported token is matched to the live state of its ``user_id``: a live key or a
role-assigned account row is left in place (``skipped_existing``); a user id with no
policy row is minted fresh; an orphaned policy row (its identity record gone after a
partial restore) is re-minted onto the surviving policy, which stays byte-equal. The
principals restore writes through the management wrappers, and the whole import
invalidates the policy cache once.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM
from tai42_contract.access_control.identity import ApiKeyIdentityProvider
from tai42_kit.access_control import registry

from tai42_skeleton.access_control import backup as ac_backup
from tai42_skeleton.access_control import management
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control.store import PostgresAccessControlStore

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
    saved = dict(registry._PROVIDERS._generation.committed())
    try:
        yield
    finally:
        registry._PROVIDERS._generation.committed().clear()
        registry._PROVIDERS._generation.committed().update(saved)


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
    registry._PROVIDERS._generation.committed()["redis"] = lambda _settings: spy
    monkeypatch.setattr(management, "client_ctx", make_access_control_client_ctx(FakeAccessControlRedis()))
    return spy


def _token(user_id: str, description: str = "restored", owner: str = "owner-1") -> dict:
    # Every api key belongs to a principal, so an exported token carries its owner claim.
    return {
        "user_id": user_id,
        "description": description,
        "scopes": ["*"],
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

    report = await ac_backup.import_access_control({"tokens": [_token("orphan")]}, "skip")

    # The orphan is re-minted: a fresh identity carrying the re-homed owner claim, the raw
    # key surfaced once, and the surviving policy row byte-equal.
    assert report.created == 1
    assert report.details["skipped_existing"] == 0
    assert report.details["new_api_keys"] == [{"user_id": "orphan", "description": "restored", "api_key": "sk-orphan"}]
    assert provider.identities["orphan"] == "restored"
    assert provider.provision_owners["orphan"] == owner
    assert pg.policy_body("orphan") == body_before


async def test_import_skips_a_live_key(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    await management.add_user_api_key("live", "live-desc", ["*"], owner_user_id="owner-1")
    report = await ac_backup.import_access_control({"tokens": [_token("live")]}, "skip")
    # Policy AND identity present: left in place, never re-minted.
    assert report.details["skipped_existing"] == 1
    assert report.created == 0
    assert report.details["new_api_keys"] == []
    assert provider.identities["live"] == "live-desc"


async def test_import_skips_an_account_row_without_minting(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    pg.add_policy("account", scopes=["hooks"])
    report = await ac_backup.import_access_control({"tokens": [_token("account")]}, "skip")
    # A role-assigned account row (no fingerprint, no identity) is never overwritten with a key.
    assert report.details["skipped_existing"] == 1
    assert report.created == 0
    assert "account" not in provider.identities
    assert pg.policy_body("account")["scopes"] == ["hooks"]


async def test_import_mints_an_absent_token(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    report = await ac_backup.import_access_control({"tokens": [_token("fresh")]}, "skip")
    # No policy row for this user id: a brand-new key is minted.
    assert report.created == 1
    assert report.details["skipped_existing"] == 0
    assert report.details["new_api_keys"] == [{"user_id": "fresh", "description": "restored", "api_key": "sk-fresh"}]
    assert provider.identities["fresh"] == "restored"


# -- principals travel with the section --------------------------------------


async def test_export_carries_principals_with_their_policy(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    pg.add_policy("owner-1", scopes=["*"])
    doc = await ac_backup.export_access_control()
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
    report = await ac_backup.import_access_control(payload, "skip")
    created = pg.principal("owner-2")
    assert created is not None
    assert created["kind"] == "service"
    assert created["created_by"] == "owner-1"
    # The token minted, owned by the just-restored principal.
    assert provider.identities["k1"] == "restored"
    assert any(row["user_id"] == "k1" for row in report.details["new_api_keys"])


async def test_import_restores_a_key_owned_by_a_disabled_principal(
    pg: FakeAccessControlPg, provider: _SpyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A key keeps living when its owner principal is disabled after the mint, so an archive
    # can hold a disabled principal with a key it owns. The restore re-mints that key while
    # the principal stays disabled, as archived: it is never enabled, not even for the mint.
    payload = {
        "principals": [
            {
                "user_id": "owner-2",
                "kind": "service",
                "display_name": "Owner Two",
                "created_by": "owner-1",
                "disabled": True,
                "created_at": None,
                "policy": {"scopes": ["*"], "policy_data": {}, "condition": None},
            }
        ],
        "tokens": [_token("k2", owner="owner-2")],
    }
    owner_disabled_at_mint: list[bool] = []
    spy_provision = provider.provision

    async def _provision(user_id: str, description: str, *, owner_user_id: str | None = None) -> str:
        owner_disabled_at_mint.append(pg.principal("owner-2")["disabled"])
        return await spy_provision(user_id, description, owner_user_id=owner_user_id)

    monkeypatch.setattr(provider, "provision", _provision)
    report = await ac_backup.import_access_control(payload, "skip")
    assert report.errors == []
    assert owner_disabled_at_mint == [True]
    assert provider.identities["k2"] == "restored"
    assert provider.provision_owners["k2"] == "owner-2"
    assert any(row["user_id"] == "k2" for row in report.details["new_api_keys"])
    assert pg.principal("owner-2")["disabled"] is True
    assert pg.policy_body("owner-2")["policy_data"] == {"disabled": True}


async def test_an_import_that_raises_at_a_key_mint_leaves_an_archived_disabled_principal_disabled(
    pg: FakeAccessControlPg, provider: _SpyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A provider fault at the key mint propagates out of the import; the principal restored
    # before it keeps the archived disabled state rather than coming back enabled.
    payload = {
        "principals": [
            {
                "user_id": "owner-2",
                "kind": "service",
                "display_name": "Owner Two",
                "created_by": "owner-1",
                "disabled": True,
                "created_at": None,
                "policy": {"scopes": ["*"], "policy_data": {}, "condition": None},
            }
        ],
        "tokens": [_token("k2", owner="owner-1")],
    }

    async def _provider_down(user_id: str, description: str, *, owner_user_id: str | None = None) -> str:
        raise RuntimeError("identity store unavailable")

    monkeypatch.setattr(provider, "provision", _provider_down)
    with pytest.raises(RuntimeError, match="identity store unavailable"):
        await ac_backup.import_access_control(payload, "skip")
    assert pg.principal("owner-2")["disabled"] is True
    assert pg.policy_body("owner-2")["policy_data"] == {"disabled": True}


async def test_import_reports_a_mapping_of_a_route_public_by_its_declaration(
    pg: FakeAccessControlPg, provider: _SpyProvider
) -> None:
    # The scope writer refuses a declared-public route; the restore names it and restores the rest.
    pg.add_route("/api/things", "things")
    report = await ac_backup.import_access_control({"scopes": {"/health": "studio", "/api/x": "things"}}, "skip")
    assert report.errors == [
        "route '/health': '/health' is public by its own declaration and stays open to everyone whatever "
        "scope it is mapped to, so it cannot be mapped into scope 'studio'"
    ]
    assert pg.route("/health") is None
    assert pg.route("/api/x")["scope_id"] == "things"


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
    report = await ac_backup.import_access_control(payload, "skip")
    assert report.details["skipped_existing"] == 1
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
    report = await ac_backup.import_access_control(payload, "skip")
    assert report.created == 1
    assert report.errors == []
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
    report = await ac_backup.import_access_control(payload, "skip")
    assert report.created == 0
    assert report.skipped == 1
    assert any("no owner claim" in err for err in report.errors)
    assert "k1" not in provider.identities


async def test_import_token_carrying_a_disabled_claim_is_a_loud_per_token_error(
    pg: FakeAccessControlPg, provider: _SpyProvider
) -> None:
    # A key has no ``disabled`` state of its own (it is revoked; a principal is disabled
    # through its own archive entry), so the mint refuses the claim and nothing is restored.
    token = _token("k3")
    token["policy_data"]["disabled"] = True
    report = await ac_backup.import_access_control({"tokens": [token]}, "skip")
    assert report.created == 0
    assert report.skipped == 1
    assert any("'disabled' is not policy content" in err for err in report.errors)
    assert pg.policy("k3") is None
    assert "k3" not in provider.identities


# -- export shape --------------------------------------------------------------

# The export of the fixture deployment built by ``_seed_export_fixture``, serialized with
# sorted keys: a principal with no policy row, an admin-shaped owner, a disabled service
# principal with a role pointer and a condition, a public and a dynamic route, one live key
# and one orphaned minted row.
_GOLDEN_EXPORT = (
    '{"patterns": {"/api/things/{id}": "^/api/things/[^/]+$"}, "principals": [{"created_at": '
    '"2026-01-02T03:04:05+00:00", "created_by": "owner-1", "disabled": false, "display_name": "Bare One", '
    '"kind": "service", "policy": {"condition": null, "policy_data": {}, "scopes": []}, "user_id": "bare-1"}, '
    '{"created_at": "2026-01-02T03:04:05+00:00", "created_by": null, "disabled": false, "display_name": '
    '"Owner One", "kind": "human", "policy": {"condition": null, "policy_data": {}, "scopes": ["*"]}, '
    '"user_id": "owner-1"}, {"created_at": "2026-01-02T03:04:05+00:00", "created_by": "owner-1", "disabled": '
    'true, "display_name": "Service One", "kind": "service", "policy": {"condition": {"text": "true"}, '
    '"policy_data": {"disabled": true, "role": "editor"}, "scopes": ["*"]}, "user_id": "svc-1"}], "scopes": '
    '{"/api/open": "public", "/api/things": "things", "/api/things/{id}": "things"}, "tokens": [{"condition": '
    'null, "description": "first key", "orphaned": false, "policy_data": {"key_fingerprint": "fp-key-1", '
    '"owner_user_id": "owner-1"}, "scopes": ["things"], "user_id": "key-1"}, {"condition": null, '
    '"description": "", "orphaned": true, "policy_data": {"key_fingerprint": "fp-o", "owner_user_id": '
    '"owner-1"}, "scopes": ["things"], "user_id": "orphan-1"}]}'
)


async def _seed_export_fixture(pg: FakeAccessControlPg) -> None:
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    pg.add_principal("svc-1", kind="service", display_name="Service One", created_by="owner-1", disabled=True)
    pg.add_principal("bare-1", kind="service", display_name="Bare One", created_by="owner-1")
    for row in pg.principals:
        row["created_at"] = stamp
    pg.add_policy("owner-1", scopes=["*"])
    pg.add_policy("svc-1", scopes=["*"], policy_data={"role": "editor", "disabled": True}, condition={"text": "true"})
    pg.add_route("/api/things", "things")
    pg.add_route("/api/things/{id}", "things", pattern="^/api/things/[^/]+$")
    pg.add_route("/api/open", "public")
    await management.add_user_api_key("key-1", "first key", ["things"], owner_user_id="owner-1")
    pg.policy("key-1")["policy_data"][KEY_FINGERPRINT_CLAIM] = "fp-key-1"
    pg.add_policy(
        "orphan-1", scopes=["things"], policy_data={KEY_FINGERPRINT_CLAIM: "fp-o", OWNER_USER_ID_CLAIM: "owner-1"}
    )


async def test_export_is_byte_equal_to_the_golden_document(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    await _seed_export_fixture(pg)
    doc = await ac_backup.export_access_control()
    assert json.dumps(doc, sort_keys=True) == _GOLDEN_EXPORT


async def test_export_default_policy_body_is_a_fresh_copy(pg: FakeAccessControlPg, provider: _SpyProvider) -> None:
    pg.add_principal("bare-2", kind="service", display_name="Bare Two")
    doc = await ac_backup.export_access_control()
    exported = next(p for p in doc["principals"] if p["user_id"] == "bare-2")
    exported["policy"]["scopes"].append("mutated")
    assert ac_backup.DEFAULT_POLICY_BODY["scopes"] == []


# -- restore call sequence and the one invalidation ----------------------------


class _Recorder:
    """Records the store calls and policy-version bumps an import makes, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []


_RECORDED_STORE_METHODS = (
    "get_principal",
    "create_principal",
    "get_policy_body",
    "create_policy",
    "set_principal_disabled",
    "add_url_to_scope",
    "pin_route_public",
)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch, pg: FakeAccessControlPg, provider: _SpyProvider) -> _Recorder:
    rec = _Recorder()
    for name in _RECORDED_STORE_METHODS:
        original = getattr(PostgresAccessControlStore, name)

        def _make(method_name: str, method: Any) -> Any:
            async def _spy(self: Any, *args: Any) -> Any:
                rec.calls.append((method_name, *args))
                return await method(self, *args)

            return _spy

        monkeypatch.setattr(PostgresAccessControlStore, name, _make(name, original))
    original_bump = management.bump_policy_version

    async def _bump() -> int:
        rec.calls.append(("bump",))
        return await original_bump()

    monkeypatch.setattr(management, "bump_policy_version", _bump)
    return rec


def _principal_entry(user_id: str, *, disabled: bool = False, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "kind": "service",
        "display_name": user_id,
        "created_by": "owner-1",
        "disabled": disabled,
        "created_at": None,
        "policy": policy if policy is not None else {"scopes": ["*"], "policy_data": {}, "condition": None},
    }


async def test_restore_store_calls_in_order_with_one_bump_at_the_end(recorder: _Recorder) -> None:
    payload = {
        "principals": [_principal_entry("p-new", disabled=True)],
        "scopes": {"/api/things": "things"},
        "tokens": [],
    }
    report = await ac_backup.import_access_control(payload, "skip")
    assert report.errors == []
    assert recorder.calls == [
        ("get_principal", "p-new"),
        ("create_principal", "p-new", "service", "p-new", "owner-1"),
        ("get_policy_body", "p-new"),
        ("create_policy", "p-new", ["*"], {}, None),
        ("set_principal_disabled", "p-new", True),
        ("add_url_to_scope", "things", "/api/things", None),
        ("bump",),
    ]


async def test_restore_bumps_once_when_a_write_raises(
    recorder: _Recorder, pg: FakeAccessControlPg, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(self: Any, *args: Any) -> None:
        raise RuntimeError("route table unavailable")

    monkeypatch.setattr(PostgresAccessControlStore, "add_url_to_scope", _boom)
    payload = {"principals": [_principal_entry("p-raise")], "scopes": {"/api/things": "things"}, "tokens": []}
    with pytest.raises(RuntimeError, match="route table unavailable"):
        await ac_backup.import_access_control(payload, "skip")
    assert pg.principal("p-raise") is not None
    assert [call for call in recorder.calls if call == ("bump",)] == [("bump",)]
    assert recorder.calls[-1] == ("bump",)


async def test_disabled_only_restore_flips_the_principal_and_bumps_once(
    recorder: _Recorder, pg: FakeAccessControlPg
) -> None:
    # The principal row is gone, its policy row survives; the archived principal is disabled.
    pg.add_policy("p-dis", scopes=["*"], policy_data={"role": "editor"})
    payload = {"principals": [_principal_entry("p-dis", disabled=True, policy={"scopes": [], "policy_data": {}})]}
    report = await ac_backup.import_access_control(payload, "skip")
    assert report.created == 1
    assert ("set_principal_disabled", "p-dis", True) in recorder.calls
    assert not any(call[0] == "create_policy" for call in recorder.calls)
    assert [call for call in recorder.calls if call == ("bump",)] == [("bump",)]
    assert pg.principal("p-dis")["disabled"] is True
    assert pg.policy_body("p-dis")["policy_data"] == {"role": "editor", "disabled": True}


async def test_enabled_restore_drops_a_marker_carried_by_the_archived_policy(
    recorder: _Recorder, pg: FakeAccessControlPg
) -> None:
    # The archive's ``disabled`` field is the authority; the policy's marker is derived from it.
    policy = {"scopes": ["*"], "policy_data": {"disabled": True, "role": "editor"}, "condition": None}
    payload = {"principals": [_principal_entry("p-on", disabled=False, policy=policy)]}
    report = await ac_backup.import_access_control(payload, "skip")
    assert report.errors == []
    assert ("set_principal_disabled", "p-on", False) in recorder.calls
    assert pg.principal("p-on")["disabled"] is False
    assert pg.policy_body("p-on")["policy_data"] == {"role": "editor"}


async def test_enabled_restore_clears_a_stale_marker_on_a_surviving_policy_row(
    recorder: _Recorder, pg: FakeAccessControlPg
) -> None:
    pg.add_policy("p-live", scopes=["*"], policy_data={"role": "editor", "disabled": True})
    payload = {"principals": [_principal_entry("p-live", disabled=False)]}
    report = await ac_backup.import_access_control(payload, "skip")
    assert report.created == 1
    assert not any(call[0] == "create_policy" for call in recorder.calls)
    assert pg.principal("p-live")["disabled"] is False
    assert pg.policy_body("p-live")["policy_data"] == {"role": "editor"}


async def test_existing_principal_restore_writes_nothing_and_does_not_bump(recorder: _Recorder) -> None:
    payload = {"principals": [_principal_entry("owner-1")]}
    report = await ac_backup.import_access_control(payload, "skip")
    assert report.details["skipped_existing"] == 1
    assert recorder.calls == [("get_principal", "owner-1")]


# -- the three management wrappers each record their change --------------------


async def _bumps_of(recorder: _Recorder) -> int:
    return sum(1 for call in recorder.calls if call == ("bump",))


async def test_create_principal_row_marks_the_batch_changed(recorder: _Recorder, pg: FakeAccessControlPg) -> None:
    async with management.policy_write_batch():
        await management.create_principal_row("w-1", "service", "W One", "owner-1")
        assert await _bumps_of(recorder) == 0
    assert await _bumps_of(recorder) == 1
    assert pg.principal("w-1")["created_by"] == "owner-1"


async def test_create_principal_policy_marks_the_batch_changed(recorder: _Recorder, pg: FakeAccessControlPg) -> None:
    async with management.policy_write_batch():
        await management.create_principal_policy("w-2", ["*"], {"k": "v"}, None)
        assert await _bumps_of(recorder) == 0
    assert await _bumps_of(recorder) == 1
    assert pg.policy_body("w-2") == {"scopes": ["*"], "policy_data": {"k": "v"}, "condition": None}


async def test_set_principal_disabled_row_marks_the_batch_changed(recorder: _Recorder, pg: FakeAccessControlPg) -> None:
    pg.add_policy("owner-1", scopes=["*"])
    async with management.policy_write_batch():
        await management.set_principal_disabled_row("owner-1", True)
        assert await _bumps_of(recorder) == 0
    assert await _bumps_of(recorder) == 1
    assert pg.principal("owner-1")["disabled"] is True


async def test_each_wrapper_bumps_at_once_outside_a_batch(recorder: _Recorder, pg: FakeAccessControlPg) -> None:
    await management.create_principal_row("w-3", "service", "W Three", None)
    assert await _bumps_of(recorder) == 1
    await management.create_principal_policy("w-3", [], {}, None)
    assert await _bumps_of(recorder) == 2
    await management.set_principal_disabled_row("w-3", True)
    assert await _bumps_of(recorder) == 3
