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

import copy
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.errors import ClientDisconnectedError
from tai42_contract.settings_profiles import SettingsProfileBody
from tai42_contract.settings_profiles.errors import SettingsProfileNotFoundError
from tai42_contract.versioning import VersionedStore
from tai42_contract.versioning.errors import (
    DocumentExistsError,
    DocumentNotFoundError,
    DocumentVersionNotFoundError,
)
from tai42_contract.versioning.models import DocumentRecord, DocumentVersion

from tai42_skeleton.app import graceful_exit, instance
from tai42_skeleton.app.bus import WorkerRow
from tai42_skeleton.app.recycle import RECYCLED, TIMED_OUT, RecycleReport, RecycleRow, RecycleStop
from tai42_skeleton.config.deployment_env import fleet_deployment_ownership
from tai42_skeleton.config.service import ConfigService, ProfileApplyOutcome
from tai42_skeleton.operations import BadRequestError, NotFoundError, NotSupportedError, OperationResponse
from tai42_skeleton.operations import config as config_ops
from tai42_skeleton.settings_profiles.store import SettingsProfileStoreView

from .._fakes.bus import FakeBus
from .._fakes.combo_check_app import CHECK_DOCUMENT, booted_check_app, clean_server
from .._fakes.write_span import ExclusiveWriteStore
from .._helpers import recapture_deployment_env
from ..config.fake_pipeline import ComboChecks


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
    # ``put_profile`` and ``diff_profile`` judge ownership over this process's own deployment,
    # a one-worker fleet, so only an X-band, deployment-set or dangling payload is refused.
    bus = FakeBus()
    monkeypatch.setattr(instance.app, "_bus", bus)
    monkeypatch.setattr(
        config_ops.ConfigService,
        "from_app",
        classmethod(
            lambda cls: ConfigService(config_manager=_FakeManager(), admin=None, bus=bus, combo_check=ComboChecks())  # type: ignore[arg-type]
        ),
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
    # A profile carrying a deployment X-band key (the readiness sentinel path) is refused at
    # save time, naming the key — never persisted.
    with pytest.raises(BadRequestError, match="TAI_READY_SENTINEL_PATH"):
        await config_ops.put_profile("p", **_body(env={"TAI_READY_SENTINEL_PATH": "/tmp/ready"}, secret_keys=[]))


async def test_put_refuses_a_dangling_env_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # A manifest !ENV marker referencing a key the profile does not carry dangles: the
    # replace deletes every non-carried stored key, so dropping ${NEEDED} is refused.
    class _Manager(_FakeManager):
        def read_manifest_preserved(self) -> dict[str, Any]:
            return {"mcp": {"url": "!ENV ${NEEDED}"}}

    monkeypatch.setattr(
        config_ops.ConfigService,
        "from_app",
        classmethod(
            lambda cls: ConfigService(config_manager=_Manager(), admin=None, bus=FakeBus(), combo_check=ComboChecks())  # type: ignore[arg-type]
        ),
    )
    with pytest.raises(BadRequestError, match="NEEDED"):
        await config_ops.put_profile("p", **_body(env={"OTHER": "1"}, secret_keys=[]))


async def test_put_refuses_a_key_the_deployment_sets_naming_it(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    # The deployment's value wins over a stored one, so a profile that would set a key the
    # deployment sets could never apply: it is refused at save time and never persisted.
    monkeypatch.setenv("PROFILE_PUT_DEPLOYED", "deployed")
    recapture_deployment_env()

    with pytest.raises(BadRequestError, match=r"set by the deployment: PROFILE_PUT_DEPLOYED"):
        await config_ops.put_profile("p", **_body(env={"PROFILE_PUT_DEPLOYED": "profile"}, secret_keys=[]))
    with pytest.raises(SettingsProfileNotFoundError):
        await view.get_profile("p")


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
        "p",
        SettingsProfileBody(
            description="d",
            env={
                "KEEP": "1",
                "CH": "new",
                "ADD": "a",
                "TAI_BUS_REDIS_URL": "redis://bus.invalid/1",
                "TAI_BUS_NAMESPACE": "other-fleet",
                "DEPLOYED_SET": "from-profile",
                "DEPLOYED_CARRIED": "same",
            },
            secret_keys=[],
        ),
    )
    monkeypatch.setenv("DEPLOYED_SET", "from-deployment")
    monkeypatch.setenv("DEPLOYED_CARRIED", "from-deployment")
    monkeypatch.setenv("DEPLOYED_DROPPED", "from-deployment")
    monkeypatch.delenv("TAI_BUS_NAMESPACE", raising=False)
    recapture_deployment_env()
    monkeypatch.setattr(
        config_ops,
        "_stored_env",
        lambda: {
            "KEEP": "1",
            "CH": "old",
            "GONE": "g",
            "TAI_BUS_REDIS_URL": "redis://bus.invalid/0",
            "DEPLOYED_CARRIED": "same",
            "DEPLOYED_DROPPED": "stale",
        },
    )
    # Deterministic classification: CH is a recycle-class key.
    monkeypatch.setattr(config_ops, "_recycle_class_keys", lambda keys: sorted(key for key in keys if key == "CH"))
    diff = await config_ops.diff_profile("p")
    assert diff["added"] == ["ADD", "DEPLOYED_SET", "TAI_BUS_NAMESPACE"]
    assert diff["removed"] == ["DEPLOYED_DROPPED", "GONE"]
    assert diff["changed"] == [
        {"key": "CH", "old": "old", "new": "new"},
        {"key": "TAI_BUS_REDIS_URL", "old": "redis://bus.invalid/0", "new": "redis://bus.invalid/1"},
    ]
    assert diff["recycle_keys"] == ["CH"]
    # The bus URL and namespace are refused on every deployment, and a key the deployment sets is
    # refused when the profile sets it; dropping or carrying a stored copy of one is not.
    assert diff["refused_keys"] == ["DEPLOYED_SET", "TAI_BUS_NAMESPACE", "TAI_BUS_REDIS_URL"]


async def test_diff_classifies_a_key_under_any_spelling_the_settings_layer_reads(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    # The settings layer reads env names case-insensitively, and a nested group under its JSON
    # name: each is the same setting as its upper-case field name.
    import tai42_skeleton.app.bus_settings  # noqa: F401  — registers the bus settings groups

    await view.create_profile(
        "p",
        SettingsProfileBody(
            description="d",
            env={
                "tai_bus_namespace": "other-fleet",
                "TAI_BUS_REDIS": '{"redis_url": "redis://bus.invalid/3"}',
                "tai_bus_redis_max_connections": "9",
            },
            secret_keys=[],
        ),
    )
    monkeypatch.delenv("TAI_BUS_NAMESPACE", raising=False)
    monkeypatch.delenv("TAI_BUS_REDIS", raising=False)
    monkeypatch.delenv("TAI_BUS_REDIS_MAX_CONNECTIONS", raising=False)
    recapture_deployment_env()
    monkeypatch.setattr(config_ops, "_stored_env", dict)
    diff = await config_ops.diff_profile("p")
    assert diff["recycle_keys"] == ["TAI_BUS_REDIS", "tai_bus_namespace", "tai_bus_redis_max_connections"]
    assert diff["refused_keys"] == ["TAI_BUS_REDIS", "tai_bus_namespace"]


async def test_diff_classifies_a_dropped_dead_copy_as_no_recycle(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    # The deployment sets a recycle-class key whose stale copy the profile drops: the row is
    # removed, but the reload produces no change, so nothing recycles and nothing is refused.
    await view.create_profile("p", SettingsProfileBody(description="d", env={"KEEP": "1"}, secret_keys=[]))
    monkeypatch.setenv("DEPLOYED_RECYCLE", "from-deployment")
    recapture_deployment_env()
    monkeypatch.setattr(config_ops, "_stored_env", lambda: {"KEEP": "1", "DEPLOYED_RECYCLE": "stale"})
    monkeypatch.setattr(
        config_ops, "_recycle_class_keys", lambda keys: sorted(key for key in keys if key == "DEPLOYED_RECYCLE")
    )
    diff = await config_ops.diff_profile("p")
    assert diff["removed"] == ["DEPLOYED_RECYCLE"]
    assert diff["recycle_keys"] == []
    assert diff["refused_keys"] == []


async def test_diff_previews_the_marks_the_apply_writes(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    # The diff previews the env the apply writes: the profile's secret_keys become the marks
    # variable, so unchanged marks are no diff and changed marks show their new value.
    await view.create_profile("same", SettingsProfileBody(description="d", env={"FOO": "x"}, secret_keys=["FOO"]))
    await view.create_profile(
        "more", SettingsProfileBody(description="d", env={"FOO": "x"}, secret_keys=["FOO", "BAR"])
    )
    monkeypatch.setattr(config_ops, "_stored_env", lambda: {"FOO": "x", "TAI_ENV_SECRET_KEYS": "FOO"})
    monkeypatch.setattr(config_ops, "_recycle_class_keys", lambda keys: [])
    same = await config_ops.diff_profile("same")
    assert (same["added"], same["removed"], same["changed"]) == ([], [], [])
    more = await config_ops.diff_profile("more")
    assert more["changed"] == [{"key": "TAI_ENV_SECRET_KEYS", "old": "FOO", "new": "FOO,BAR"}]


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


# -- apply --------------------------------------------------------------


def _stub_apply_service(monkeypatch: pytest.MonkeyPatch, outcome: ProfileApplyOutcome) -> dict[str, Any]:
    """Wire ``ConfigService.from_app`` to a stub whose ``apply_replace_env`` records its
    call and returns ``outcome`` — so the op-edge arming decision is asserted without the
    real pipeline (its own contract lives in ``tests/config/test_profile_apply``)."""
    seen: dict[str, Any] = {}

    class _Svc:
        async def apply_replace_env(self, env, *, save_previous):  # type: ignore[no-untyped-def]
            seen["env"] = env
            seen["save_previous"] = save_previous
            return outcome

    monkeypatch.setattr(config_ops.ConfigService, "from_app", classmethod(lambda cls: _Svc()))
    return seen


def _apply_outcome(
    *, serve_affecting: bool, recycle: RecycleReport | None = None, span_release_error: str | None = None
) -> ProfileApplyOutcome:
    from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind

    return ProfileApplyOutcome(
        hot=["A"],
        recycle=recycle,
        self_identity=WorkerIdentity(name="serve-applier", kind=WorkerKind.serve, pid=1, generation=1),
        serve_affecting=serve_affecting,
        fleet=FleetResult(op="reload_config", results=[]),
        span_release_error=span_release_error,
    )


def _converged() -> RecycleReport:
    return RecycleReport(rows=[RecycleRow(name="backend-1", kind="backend", generation_before=1, status=RECYCLED)])


async def test_apply_arms_self_exit_when_serve_affecting(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"}, secret_keys=[])))
    seen = _stub_apply_service(monkeypatch, _apply_outcome(serve_affecting=True, recycle=_converged()))
    # The armed self-exit names this process's bus member, so the test runs with its own bus.
    bus = FakeBus(origin="serve-test")
    monkeypatch.setattr(instance.app, "_bus", bus)
    response = await config_ops.apply_profile("p")
    assert isinstance(response, OperationResponse)
    # A serve-affecting recycle arms the applier's OWN deferred self-exit as a post-flush
    # BackgroundTask wrapping ``request_graceful_exit`` — never an inline create_task.
    assert response.background is not None
    assert response.background.func is graceful_exit.request_graceful_exit
    assert response.background.args == (bus.member_identity.pid,)
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


async def test_apply_reports_the_store_span_release_error(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"}, secret_keys=[])))
    error = "OSError: releasing the span failed"
    _stub_apply_service(monkeypatch, _apply_outcome(serve_affecting=False, span_release_error=error))
    response = await config_ops.apply_profile("p")
    assert isinstance(response, OperationResponse)
    assert response.payload["span_release_error"] == error


async def test_apply_does_not_arm_self_exit_when_the_roll_stopped(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"}, secret_keys=[])))
    stopped = RecycleReport(
        rows=[
            RecycleRow(
                name="backend-1",
                kind="backend",
                generation_before=1,
                status=TIMED_OUT,
                detail="old life still present",
            )
        ]
    )
    _stub_apply_service(monkeypatch, _apply_outcome(serve_affecting=True, recycle=stopped))
    response = await config_ops.apply_profile("p")
    assert isinstance(response, OperationResponse)
    assert response.background is None
    assert response.payload["recycle"] == [
        {
            "name": "backend-1",
            "kind": "backend",
            "status": "timed-out",
            "generation_before": 1,
            "detail": "old life still present",
        }
    ]


async def test_apply_does_not_arm_self_exit_when_the_roll_stopped_with_no_target_in_hand(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"}, secret_keys=[])))
    stopped = _converged()
    stopped.stopped = RecycleStop(kind="backend", name=None, detail="bus unreachable while reading the census")
    _stub_apply_service(monkeypatch, _apply_outcome(serve_affecting=True, recycle=stopped))
    response = await config_ops.apply_profile("p")
    assert isinstance(response, OperationResponse)
    assert response.background is None
    assert response.payload["recycle_stopped"] == {
        "kind": "backend",
        "name": None,
        "detail": "bus unreachable while reading the census",
    }


async def test_apply_absent_is_404() -> None:
    with pytest.raises(NotFoundError):
        await config_ops.apply_profile("nope")


@pytest.mark.parametrize(
    ("stored", "expected_env", "expected_marks"),
    [
        ({"API_KEY": "v", "TAI_ENV_SECRET_KEYS": "API_KEY"}, {"API_KEY": "v"}, ["API_KEY"]),
        ({"API_KEY": "v"}, {"API_KEY": "v"}, []),
    ],
    ids=["with-marks-row", "without-marks-row"],
)
async def test_save_previous_version_snapshots_the_stored_band_as_a_profile(
    monkeypatch: pytest.MonkeyPatch,
    view: SettingsProfileStoreView,
    stored: dict[str, str],
    expected_env: dict[str, str],
    expected_marks: list[str],
) -> None:
    # The @previous snapshot is the pre-apply stored env in the profile shape: the rows without
    # the marks variable, the band's own marks in ``secret_keys``. Applying it writes the
    # pre-apply band back exactly, whatever the live manifest's connectors declare.
    live_manifest = {"connectors": [{"id": "acme", "kind": "oauth", "client_secret_env": "ACME_CLIENT_SECRET"}]}
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(admin=SimpleNamespace(live_manifest=live_manifest)))
    await config_ops._save_previous_version(dict(stored))

    body = await view.get_active_body("@previous")
    assert body.env == expected_env
    assert body.secret_keys == expected_marks
    assert config_ops._profile_env(body) == stored


async def test_apply_maps_refusal_to_400(monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"}, secret_keys=[])))

    class _Svc:
        async def apply_replace_env(self, env, *, save_previous):  # type: ignore[no-untyped-def]
            raise ValueError("Refusing to apply: SUB_MCP_REDIS_URL is set by the deployment.")

    monkeypatch.setattr(config_ops.ConfigService, "from_app", classmethod(lambda cls: _Svc()))
    with pytest.raises(BadRequestError, match="SUB_MCP_REDIS_URL"):
        await config_ops.apply_profile("p")


async def test_apply_carries_the_profile_secret_marks_into_the_replaced_env(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    # A profile saved with its marks in ``secret_keys`` (the env rows never carry the marks
    # variable) keeps those keys masked once applied: the replace writes the marks with the env.
    await view.create_profile("p", SettingsProfileBody(**_body(env={"FOO": "x"}, secret_keys=["FOO"])))
    seen = _stub_apply_service(monkeypatch, _apply_outcome(serve_affecting=False))
    await config_ops.apply_profile("p")
    assert seen["env"] == {"FOO": "x", "TAI_ENV_SECRET_KEYS": "FOO"}


async def test_apply_unions_marks_already_in_the_profile_env(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    body = _body(env={"FOO": "x", "BAR": "y", "TAI_ENV_SECRET_KEYS": "BAR"}, secret_keys=["FOO"])
    await view.create_profile("p", SettingsProfileBody(**body))
    seen = _stub_apply_service(monkeypatch, _apply_outcome(serve_affecting=False))
    await config_ops.apply_profile("p")
    assert seen["env"]["TAI_ENV_SECRET_KEYS"] == "BAR,FOO"


async def test_apply_without_marks_writes_no_marks_variable(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"A": "1"}, secret_keys=[])))
    seen = _stub_apply_service(monkeypatch, _apply_outcome(serve_affecting=False))
    await config_ops.apply_profile("p")
    assert seen["env"] == {"A": "1"}


# -- the extension combo gate ------------------------------------------------

_REFUSAL = "tools[a].extensions[echo][0]: extension 'refusing' on tool 'echo' cannot bind: bad label"


class _MarkedManager(ExclusiveWriteStore):
    """A deployment whose manifest binds ``refusing`` on ``echo`` with its setting behind ``!ENV ${EXT_L}``."""

    def read_env(self) -> dict[str, str]:
        return {"EXT_L": "ok"}

    def read_manifest_preserved(self) -> dict[str, Any]:
        document = copy.deepcopy(CHECK_DOCUMENT)
        document["tools"][0]["extensions"] = {"echo": [[{"name": "refusing", "config": {"label": "!ENV ${EXT_L}"}}]]}
        return document


def _live_check_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the booted check app's combo check through a service over a one-worker fleet.

    The booted app joins the bus its boot builds; the service judges ownership over its own.
    """
    monkeypatch.setattr(instance.app, "_bus", None)
    monkeypatch.setattr(
        config_ops.ConfigService,
        "from_app",
        classmethod(
            lambda cls: ConfigService(
                config_manager=_MarkedManager(),  # type: ignore[arg-type]
                admin=None,  # type: ignore[arg-type]
                bus=FakeBus(),  # type: ignore[arg-type]
                combo_check=instance.app.extensions.check_combo,
            )
        ),
    )


@pytest.mark.usefixtures(clean_server.__name__)
async def test_put_profile_refuses_an_env_the_combo_check_refuses(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    _live_check_service(monkeypatch)
    async with booted_check_app():
        with pytest.raises(BadRequestError) as caught:
            await config_ops.put_profile("p", **_body(env={"EXT_L": "x"}, secret_keys=[]))
        assert str(caught.value) == _REFUSAL
        await config_ops.put_profile("p", **_body(env={"EXT_L": "fine"}, secret_keys=[]))
    assert (await view.get_active_body("p")).env == {"EXT_L": "fine"}


@pytest.mark.usefixtures(clean_server.__name__)
async def test_apply_profile_refuses_an_env_the_combo_check_refuses(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView
) -> None:
    await view.create_profile("p", SettingsProfileBody(**_body(env={"EXT_L": "x"}, secret_keys=[])))
    _live_check_service(monkeypatch)
    async with booted_check_app():
        with pytest.raises(BadRequestError) as caught:
            await config_ops.apply_profile("p")
        assert str(caught.value) == _REFUSAL


# -- the bus cannot answer ------------------------------------------------------


class _OutageBus(FakeBus):
    """A bus that answers until ``down`` is set, then fails its census the way a severed bus does."""

    down = False

    async def census(self) -> list[WorkerRow]:
        if self.down:
            raise ClientDisconnectedError("bus down")
        return await super().census()


async def _sibling_read_then_outage(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    bus = _OutageBus(origin="serve-x", remotes=["backend-1"])
    bus.rows[1].deployment_keys = [key]
    await fleet_deployment_ownership(bus)
    bus.down = True
    monkeypatch.setattr(instance.app, "_bus", bus)
    monkeypatch.setattr(
        config_ops.ConfigService,
        "from_app",
        classmethod(
            lambda cls: ConfigService(config_manager=_FakeManager(), admin=None, bus=bus, combo_check=ComboChecks())  # type: ignore[arg-type]
        ),
    )


async def test_diff_during_an_outage_refuses_a_key_last_read_as_a_sibling_s(
    monkeypatch: pytest.MonkeyPatch, view: SettingsProfileStoreView, caplog: pytest.LogCaptureFixture
) -> None:
    await view.create_profile(
        "p", SettingsProfileBody(description="d", env={"KEEP": "1", "SIBLING_SET": "v"}, secret_keys=[])
    )
    await _sibling_read_then_outage(monkeypatch, "SIBLING_SET")
    monkeypatch.setattr(config_ops, "_stored_env", lambda: {"KEEP": "1"})
    monkeypatch.setattr(config_ops, "_recycle_class_keys", lambda keys: [])

    with caplog.at_level(logging.WARNING):
        diff = await config_ops.diff_profile("p")

    assert diff["added"] == ["SIBLING_SET"]
    assert diff["refused_keys"] == ["SIBLING_SET"]
    assert any("'diff_profile'" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_put_during_an_outage_refuses_a_key_last_read_as_a_sibling_s(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await _sibling_read_then_outage(monkeypatch, "SIBLING_SET")

    with caplog.at_level(logging.WARNING), pytest.raises(BadRequestError, match="set by the deployment: SIBLING_SET"):
        await config_ops.put_profile("p", **_body(env={"SIBLING_SET": "v"}, secret_keys=[]))

    assert any("'put_profile'" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
