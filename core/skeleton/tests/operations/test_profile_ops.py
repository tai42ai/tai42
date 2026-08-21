"""Op-level oracles for the settings-profile operations.

CRUD + diff + versions/rollback over the versioned ``settings_profile`` store. The
store is faked in-memory (a ``SettingsProfileStoreView`` over a minimal generic
store), the store-configured predicate is forced on, and ``ConfigService`` is wired
to a fake config manager so ``put_profile``'s save-time boundary validation runs the
real refusal logic. ``apply_profile`` is covered at the op edge (404, error mapping, and
the self-exit BackgroundTask arming decision) with the pipeline itself stubbed — the
pipeline's ordering / failure contract is oracled in ``tests/config/test_profile_apply``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.settings_profiles import SettingsProfileBody
from tai42_contract.versioning import VersionedStore
from tai42_contract.versioning.errors import (
    DocumentExistsError,
    DocumentNotFoundError,
    DocumentVersionNotFoundError,
)
from tai42_contract.versioning.models import DocumentRecord, DocumentVersion

from tai42_skeleton.config.recycle_policy import CapabilityReport, Shape
from tai42_skeleton.config.service import ConfigService, ProfileApplyOutcome
from tai42_skeleton.operations import BadRequestError, NotFoundError, NotSupportedError, OperationResponse
from tai42_skeleton.operations import config as config_ops
from tai42_skeleton.settings.env_secret_marks import env_secret_marks_settings
from tai42_skeleton.settings_profiles.store import SettingsProfileStoreView


class _MemStore(VersionedStore):
    """Faithful in-memory ``VersionedStore`` for op tests (one active row per key)."""

    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict[str, Any]] = {}

    async def create(self, kind, name, body, tags=None, *, tx=None) -> DocumentRecord:
        key = (kind, name)
        if key in self.docs:
            raise DocumentExistsError(kind, name)
        self.docs[key] = {"active": 1, "versions": {1: (dict(body), list(tags or []))}}
        return DocumentRecord(kind=kind, name=name, active_version=1, is_active=True, created_at="t")

    async def save_version(self, kind, name, body, tags=None, *, tx=None) -> DocumentVersion:
        doc = self.docs.get((kind, name))
        if doc is None:
            raise DocumentNotFoundError(kind, name)
        new_version = max(doc["versions"]) + 1
        doc["versions"][new_version] = (dict(body), list(tags or []))
        doc["active"] = new_version
        return DocumentVersion(version=new_version, body=body, tags=list(tags or []), created_at="t", is_current=True)

    async def list(self, kind) -> list[DocumentRecord]:
        return [
            DocumentRecord(kind=k, name=n, active_version=d["active"], is_active=True, created_at="t")
            for (k, n), d in self.docs.items()
            if k == kind
        ]

    async def get(self, kind, name) -> DocumentRecord:
        doc = self._require(kind, name)
        return DocumentRecord(kind=kind, name=name, active_version=doc["active"], is_active=True, created_at="t")

    async def get_active_body(self, kind, name, *, tx=None, for_update=False) -> dict[str, Any]:
        doc = self._require(kind, name)
        return dict(doc["versions"][doc["active"]][0])

    async def list_versions(self, kind, name) -> list[DocumentVersion]:
        doc = self._require(kind, name)
        return [
            DocumentVersion(version=v, body=body, tags=tags, created_at="t", is_current=v == doc["active"])
            for v, (body, tags) in sorted(doc["versions"].items())
        ]

    async def get_version(self, kind, name, version, *, tx=None) -> DocumentVersion:
        doc = self.docs.get((kind, name))
        if doc is None or version not in doc["versions"]:
            raise DocumentVersionNotFoundError(kind, name, version)
        body, tags = doc["versions"][version]
        return DocumentVersion(
            version=version, body=body, tags=tags, created_at="t", is_current=version == doc["active"]
        )

    async def rollback(self, kind, name, version, *, tx=None) -> DocumentRecord:
        doc = self.docs.get((kind, name))
        if doc is None or version not in doc["versions"]:
            raise DocumentVersionNotFoundError(kind, name, version)
        doc["active"] = version
        return DocumentRecord(kind=kind, name=name, active_version=version, is_active=True, created_at="t")

    async def soft_delete(self, kind, name) -> None:
        self._require(kind, name)
        del self.docs[(kind, name)]

    async def delete(self, kind, name, *, tx=None) -> None:
        self._require(kind, name)
        del self.docs[(kind, name)]

    async def rename(self, kind, name, new_name) -> DocumentRecord:  # pragma: no cover - unused
        raise NotImplementedError

    def transaction(self):  # pragma: no cover - unused by the profile view
        raise NotImplementedError

    def _require(self, kind, name) -> dict[str, Any]:
        doc = self.docs.get((kind, name))
        if doc is None:
            raise DocumentNotFoundError(kind, name)
        return doc


class _FakeManager:
    """The config-manager surface ``_validate_replace`` reads — an empty deployment
    (no stored env, no manifest) so a clean profile validates and an X-band / dangling
    payload is the only thing that can be refused."""

    def read_env(self) -> dict[str, str]:
        return {}

    def read_manifest_preserved(self) -> dict[str, Any]:
        return {}


@pytest.fixture
def view() -> SettingsProfileStoreView:
    return SettingsProfileStoreView(_MemStore())


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView) -> None:
    monkeypatch.setattr(config_ops, "component_store_configured", lambda _component: True)
    monkeypatch.setattr(config_ops, "settings_profile_store", lambda: view)
    # ``put_profile`` runs the shared boundary validator; wire it to an empty
    # deployment so only an X-band / dangling payload is refused.
    monkeypatch.setattr(
        config_ops.ConfigService,
        "from_app",
        classmethod(lambda cls: ConfigService(config_manager=_FakeManager(), admin=None, bus=None)),  # type: ignore[arg-type]
    )


def _body(*, description: str = "d", env: dict[str, str] | None = None, secret_keys=None) -> dict[str, Any]:
    return {
        "description": description,
        "env": env if env is not None else {"API_KEY": "s"},
        "secret_keys": secret_keys if secret_keys is not None else ["API_KEY"],
    }


# -- put (create / new version) ----------------------------------------------


async def test_put_creates_then_versions_and_never_emits_body() -> None:
    created = await config_ops.put_profile("p", **_body(env={"A": "1"}, secret_keys=["A"]))
    assert created == {"ok": True, "version": 1}
    # A second PUT appends a version — never re-emits the stored body/secret.
    updated = await config_ops.put_profile("p", **_body(env={"A": "2"}, secret_keys=["A"]))
    assert updated == {"ok": True, "version": 2}
    got = await config_ops.get_profile("p")
    assert got == {"description": "d", "env": {"A": "2"}, "secret_keys": ["A"]}


async def test_put_converges_on_a_concurrent_create_race(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    """A PUT of a NEW name whose existence probe sees NotFound but whose create then loses
    to a concurrent create (the store's active-name unique index) must CONVERGE by
    appending a version — never surface an opaque 500."""
    from tai42_contract.settings_profiles.errors import SettingsProfileExistsError, SettingsProfileNotFoundError

    # The concurrent winner already created "p"; give save_version a profile to append to.
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"})))

    async def _probe_stale(name: str) -> DocumentRecord:
        raise SettingsProfileNotFoundError(name)

    async def _create_loses(name: str, body: SettingsProfileBody) -> DocumentRecord:
        raise SettingsProfileExistsError(name)

    monkeypatch.setattr(view, "get_profile", _probe_stale)
    monkeypatch.setattr(view, "create_profile", _create_loses)

    # The loser falls through to save_version (real) — appends v2, no 500.
    result = await config_ops.put_profile("p", **_body(env={"A": "2"}))
    assert result == {"ok": True, "version": 2}


async def test_put_rejects_reserved_at_prefixed_name() -> None:
    with pytest.raises(BadRequestError, match="reserved"):
        await config_ops.put_profile("@previous", **_body())


async def test_put_refuses_an_x_band_key_naming_it() -> None:
    # A profile carrying a deployment X-band key (the supervision marker) is refused at
    # save time, naming the key — never persisted.
    with pytest.raises(BadRequestError, match="TAI_SUPERVISED"):
        await config_ops.put_profile("p", **_body(env={"TAI_SUPERVISED": "compose"}, secret_keys=[]))


async def test_put_refuses_a_dangling_env_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # A manifest !ENV marker referencing a key the profile does not carry dangles: the
    # replace deletes every non-carried stored key, so dropping ${NEEDED} is refused.
    class _Manager(_FakeManager):
        def read_manifest_preserved(self) -> dict[str, Any]:
            return {"mcp": {"url": "!ENV ${NEEDED}"}}

    monkeypatch.setattr(
        config_ops.ConfigService,
        "from_app",
        classmethod(lambda cls: ConfigService(config_manager=_Manager(), admin=None, bus=None)),  # type: ignore[arg-type]
    )
    with pytest.raises(BadRequestError, match="NEEDED"):
        await config_ops.put_profile("p", **_body(env={"OTHER": "1"}, secret_keys=[]))


# -- get / list / delete -----------------------------------------------------


async def test_get_absent_is_404() -> None:
    with pytest.raises(NotFoundError):
        await config_ops.get_profile("nope")


async def test_list_excludes_reserved_at_profiles(view: SettingsProfileStoreView) -> None:
    await view.create_profile("beta", SettingsProfileBody(**_body(description="B")))
    await view.create_profile("alpha", SettingsProfileBody(**_body(description="A")))
    await view.create_profile("@previous", SettingsProfileBody(**_body(description="X")))
    rows = await config_ops.list_profiles()
    assert rows == [{"name": "beta", "description": "B"}, {"name": "alpha", "description": "A"}] or rows == [
        {"name": "alpha", "description": "A"},
        {"name": "beta", "description": "B"},
    ]
    assert all(not r["name"].startswith("@") for r in rows)


async def test_delete_then_absent(view: SettingsProfileStoreView) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body()))
    assert await config_ops.delete_profile("p") == {"ok": True}
    with pytest.raises(NotFoundError):
        await config_ops.delete_profile("p")


# -- diff --------------------------------------------------------------------


async def test_diff_reports_added_removed_changed_recycle_and_refused(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    await view.create_profile(
        "p", SettingsProfileBody(description="d", env={"KEEP": "1", "CH": "new", "ADD": "a"}, secret_keys=[])
    )
    monkeypatch.setattr(config_ops, "_stored_env", lambda: {"KEEP": "1", "CH": "old", "GONE": "g"})
    # Deterministic classification: CH is a recycle-class key; GONE is refused upfront.
    monkeypatch.setattr(config_ops, "_reload_class_by_env_var", lambda: {"CH": "recycle", "KEEP": "hot"})
    monkeypatch.setattr(
        config_ops,
        "capability_report",
        lambda: CapabilityReport(shape=Shape.compose, recycle_supported=True, refused_keys=["GONE"], census_kinds=[]),
    )
    diff = await config_ops.diff_profile("p")
    assert diff["added"] == ["ADD"]
    assert diff["removed"] == ["GONE"]
    assert diff["changed"] == [{"key": "CH", "old": "old", "new": "new"}]
    assert diff["recycle_keys"] == ["CH"]
    assert diff["refused_keys"] == ["GONE"]


async def test_diff_absent_is_404() -> None:
    with pytest.raises(NotFoundError):
        await config_ops.diff_profile("nope")


# -- versions / rollback -----------------------------------------------------


async def test_list_versions_strips_body_and_marks_current(view: SettingsProfileStoreView) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"})))
    await view.save_version("p", SettingsProfileBody(**_body(env={"A": "2"})))
    rows = await config_ops.list_profile_versions("p")
    assert [(r["version"], r["is_current"]) for r in rows] == [(1, False), (2, True)]
    assert all("body" not in r for r in rows)


async def test_get_version_includes_body(view: SettingsProfileStoreView) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"}, secret_keys=["A"])))
    row = await config_ops.get_profile_version("p", "1")
    assert row["version"] == 1
    assert row["body"] == {"description": "d", "env": {"A": "1"}, "secret_keys": ["A"]}


async def test_get_version_non_integer_is_400(view: SettingsProfileStoreView) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body()))
    with pytest.raises(BadRequestError, match="integer"):
        await config_ops.get_profile_version("p", "notint")


async def test_get_version_unknown_is_404(view: SettingsProfileStoreView) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body()))
    with pytest.raises(NotFoundError):
        await config_ops.get_profile_version("p", "99")


async def test_rollback_repoints_active(view: SettingsProfileStoreView) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"})))
    await view.save_version("p", SettingsProfileBody(**_body(env={"A": "2"})))
    assert await config_ops.rollback_profile("p", 1) == {"ok": True, "version": 1}
    assert (await config_ops.get_profile("p"))["env"] == {"A": "1"}


async def test_rollback_unknown_version_is_404(view: SettingsProfileStoreView) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body()))
    with pytest.raises(NotFoundError):
        await config_ops.rollback_profile("p", 99)


# -- store-unconfigured gating -----------------------------------------------


async def test_list_empty_when_store_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_ops, "component_store_configured", lambda _component: False)
    assert await config_ops.list_profiles() == []


async def test_reads_refuse_501_when_store_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_ops, "component_store_configured", lambda _component: False)
    with pytest.raises(NotSupportedError):
        await config_ops.get_profile("p")
    with pytest.raises(NotSupportedError):
        await config_ops.put_profile("p", **_body())


# -- apply (C5) --------------------------------------------------------------


def _stub_apply_service(monkeypatch: pytest.MonkeyPatch, outcome: ProfileApplyOutcome) -> dict[str, Any]:
    """Wire ``ConfigService.from_app`` to a stub whose ``apply_replace_env`` records its
    call and returns ``outcome`` — so the op-edge arming decision is asserted without the
    real pipeline (its own contract lives in ``tests/config/test_profile_apply``)."""
    seen: dict[str, Any] = {}

    class _Svc:
        async def apply_replace_env(self, env, *, driven, save_previous):  # type: ignore[no-untyped-def]
            seen["env"] = env
            seen["driven"] = driven
            seen["save_previous"] = save_previous
            return outcome

    monkeypatch.setattr(config_ops.ConfigService, "from_app", classmethod(lambda cls: _Svc()))
    return seen


def _apply_outcome(*, serve_affecting: bool) -> ProfileApplyOutcome:
    from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind

    return ProfileApplyOutcome(
        hot=["A"],
        recycle=None,
        self_identity=WorkerIdentity(name="serve-applier", kind=WorkerKind.serve, pid=1, generation=1),
        serve_affecting=serve_affecting,
        fleet=FleetResult(op="reload_config", results=[]),
    )


async def test_apply_arms_self_exit_when_serve_affecting(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"}, secret_keys=[])))
    seen = _stub_apply_service(monkeypatch, _apply_outcome(serve_affecting=True))
    response = await config_ops.apply_profile("p")
    assert isinstance(response, OperationResponse)
    # A serve-affecting recycle arms the applier's OWN deferred self-exit as a post-flush
    # BackgroundTask wrapping ``request_serve_graceful_exit`` — never an inline create_task.
    assert response.background is not None
    assert response.background.func is config_ops.request_serve_graceful_exit
    assert response.payload["refused"] == []
    assert response.payload["hot"] == ["A"]
    # The profile's active env is what the pipeline was handed.
    assert seen["env"] == {"A": "1"}


async def test_apply_no_self_exit_when_not_serve_affecting(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"}, secret_keys=[])))
    _stub_apply_service(monkeypatch, _apply_outcome(serve_affecting=False))
    response = await config_ops.apply_profile("p")
    assert isinstance(response, OperationResponse)
    assert response.background is None  # hot-only / backend-only / bare → no self-exit


async def test_apply_absent_is_404() -> None:
    with pytest.raises(NotFoundError):
        await config_ops.apply_profile("nope")


async def test_save_previous_version_derives_connector_secret_key(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    # The @previous snapshot the apply pipeline saves carries a secret_keys set DERIVED from
    # effective_secret_keys(live_manifest): the operator's stored marks UNIONED with every
    # live oauth connector's client_secret_env, so a rollback re-applies with the connector
    # secret still masked even without an operator mark for it.
    monkeypatch.setenv("TAI_ENV_SECRET_KEYS", "API_KEY")
    env_secret_marks_settings.cache_clear()
    live_manifest = {"connectors": [{"id": "acme", "kind": "oauth", "client_secret_env": "ACME_CLIENT_SECRET"}]}
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(admin=SimpleNamespace(live_manifest=live_manifest)))
    try:
        await config_ops._save_previous_version({"API_KEY": "v"})
    finally:
        env_secret_marks_settings.cache_clear()

    body = await view.get_active_body("@previous")
    assert body.env == {"API_KEY": "v"}
    # The connector-derived key sits alongside the operator's own mark in the snapshot.
    assert set(body.secret_keys) == {"API_KEY", "ACME_CLIENT_SECRET"}


async def test_apply_maps_refusal_to_400(monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"}, secret_keys=[])))

    class _Svc:
        async def apply_replace_env(self, env, *, driven, save_previous):  # type: ignore[no-untyped-def]
            raise ValueError("Refusing to apply: SUB_MCP_REDIS_URL is deployment-pinned.")

    monkeypatch.setattr(config_ops.ConfigService, "from_app", classmethod(lambda cls: _Svc()))
    with pytest.raises(BadRequestError, match="SUB_MCP_REDIS_URL"):
        await config_ops.apply_profile("p")
