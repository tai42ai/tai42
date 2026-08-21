"""The mcp-server install/update/uninstall env pipeline over faked seams.

An mcp-server install routes through the COMBINED env+manifest pipeline: the required
``!ENV`` values land in the env store in the same transaction that writes the
``{title, config}`` entry (never deferred). This pins:

* a missing required var → a typed :class:`InstallEnvError` naming the var + pointer;
* the happy combined transaction (values + entry land together);
* a manifest-persist failure leaves NO orphan env write of either kind (the
  installer-unwind deletes exactly the value keys this install wrote AND restores the
  ``TAI_ENV_SECRET_KEYS`` marks var to its prior value, atop the pipeline's orphan
  contract);
* an upgrade to a version adding a required var is refused unless supplied;
* uninstall LEAVES the stored env values and surfaces the orphaned vars via the notes.
"""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from tai42_contract.plugins import PluginSpec

from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.config.boundary import refuse_dangling_env_markers
from tai42_skeleton.config.service import ApplyResult, OrphanEnvWriteError
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.marketplace.errors import InstallEnvError, InstallStateError
from tai42_skeleton.marketplace.installer import Installer
from tai42_skeleton.marketplace.manifest_patch import apply_provides
from tai42_skeleton.marketplace.store import InstallRecord

from ._specs import make_resolved, make_spec


def _mcp_spec(*, version: str = "1.0.0", env: dict[str, str] | None = None) -> PluginSpec:
    mcp: dict[str, Any] = {"command": "pg-mcp-server"}
    if env is not None:
        mcp["env"] = env
    return PluginSpec.model_validate(
        {
            "spec_version": 1,
            "namespace": "tai42",
            "name": "pg-mcp",
            "package": "tai42-pg-mcp",
            "version": version,
            "description": "A managed Postgres MCP server.",
            "license": "Apache-2.0",
            "categories": ["dev"],
            "provides": [{"kind": "mcp-server", "name": "postgres", "mcp": mcp, "description": "d"}],
        }
    )


def _fleet_report() -> FleetResult:
    return FleetResult(op="reload_config", results=[])


class _FakeRegistry:
    def __init__(self) -> None:
        self.resolved: dict[str, Any] | None = None

    async def resolve(self, ns: str, name: str, version: str | None = None) -> dict[str, Any]:
        assert self.resolved is not None
        return self.resolved


class _FakePip:
    async def __call__(self, args: list[str]) -> str:
        return "pip ok"


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, InstallRecord] = {}

    async def get(self, ref: str) -> InstallRecord | None:
        return self.rows.get(ref)

    async def record(self, ref, version, source, repository_url, tag, artifact_ref, sha256, spec, **kw) -> None:
        self.rows[ref] = InstallRecord(
            ref=ref,
            version=version,
            source=source,
            repository_url=repository_url,
            tag=tag,
            artifact_ref=artifact_ref,
            sha256=sha256,
            spec=spec,
            installed_at=datetime.now(UTC),
            **kw,
        )

    async def delete(self, ref: str) -> bool:
        return self.rows.pop(ref, None) is not None

    async def list_installed(self) -> list[InstallRecord]:
        return list(self.rows.values())

    def preload(self, spec: PluginSpec) -> None:
        self.rows[spec.ref] = InstallRecord(
            ref=spec.ref,
            version=spec.version,
            source="pypi",
            repository_url=None,
            tag=None,
            artifact_ref=None,
            sha256=None,
            spec=spec.model_dump(mode="json"),
            installed_at=datetime.now(UTC),
        )


class _FakeCM:
    def __init__(self, manifest: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> None:
        self._manifest = manifest or {}
        self._env = dict(env or {})

    def read_manifest_preserved(self) -> dict[str, Any]:
        return copy.deepcopy(self._manifest)

    def read_env(self) -> dict[str, str]:
        return dict(self._env)

    def write_env(self, changes: dict[str, str]) -> None:
        for key, value in changes.items():
            if value == "":
                self._env.pop(key, None)
            else:
                self._env[key] = value


class _FakeSvc:
    """Mirrors ConfigService's mcp-relevant doors closely enough to exercise the
    installer's combined-pipeline routing + unwind: ``apply_env_and_change`` validates
    the mutated manifest against the effective env (dangling → ValueError), writes the
    env FIRST, then persists the manifest — raising :class:`OrphanEnvWriteError` on a
    persist failure with the env write STANDING."""

    def __init__(self, cm: _FakeCM) -> None:
        self._cm = cm
        self.fail_persist = False
        self.env_change_calls = 0

    async def apply_change(self, mutator: Any) -> ApplyResult:
        document = copy.deepcopy(self._cm._manifest)
        mutator(document)
        self._cm._manifest = document
        return ApplyResult(fleet=_fleet_report(), local={"reloaded": True}, document=document)

    async def apply_replace(self, document: dict[str, Any]) -> ApplyResult:
        self._cm._manifest = copy.deepcopy(document)
        return ApplyResult(fleet=_fleet_report(), local={"reloaded": True}, document=document)

    def _effective_env(self, changes: dict[str, str]) -> dict[str, str]:
        import os

        return {**os.environ, **self._cm.read_env(), **changes}

    async def apply_env_change(self, changes: dict[str, str]) -> ApplyResult:
        # Counted so a test can assert the unwind fired NO env change (no fleet broadcast)
        # on a pre-persist refusal where nothing actually landed in the store.
        self.env_change_calls += 1
        self._cm.write_env(changes)
        return ApplyResult(fleet=_fleet_report(), local={"reloaded": True}, document=None)

    async def apply_env_and_change(self, prepare: Any, *, manifest_pointer: str | None = None) -> ApplyResult:
        import os

        stored = self._cm.read_env()
        changes, mutator = await prepare(stored)
        candidate = copy.deepcopy(self._cm._manifest)
        mutator(candidate)
        effective = {**os.environ, **{k: v for k, v in changes.items() if v != ""}}
        refuse_dangling_env_markers(candidate, effective)  # dangling → ValueError, before any write
        self._cm.write_env(changes)  # env-write FIRST
        if self.fail_persist:
            raise OrphanEnvWriteError(f"orphan env write for {sorted(changes)} (intended pointer {manifest_pointer!r})")
        self._cm._manifest = candidate
        return ApplyResult(fleet=_fleet_report(), local={"reloaded": True}, document=candidate)


class _FakeFleetLock:
    def __call__(self):
        return self._cm()

    @asynccontextmanager
    async def _cm(self):
        yield


class Harness:
    def __init__(self, manifest: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> None:
        self.registry = _FakeRegistry()
        self.pip = _FakePip()
        self.store = _FakeStore()
        self.cm = _FakeCM(manifest, env)
        self.svc = _FakeSvc(self.cm)

    def installer(self) -> Installer:
        return Installer(
            registry=cast(Any, self.registry),
            pip_runner=self.pip,
            store=cast(Any, self.store),
            config_service=cast(Any, self.svc),
            fleet_lock=_FakeFleetLock(),
            config_manager=self.cm,
        )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GUARD_PG_PW", raising=False)
    monkeypatch.delenv("GUARD_PG_USER", raising=False)


async def test_install_missing_required_var_is_a_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # With nothing supplied, the installer's early env pre-check (before any pip work)
    # fails loudly naming the required var, ahead of the pipeline's authoritative refusal.
    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${GUARD_PG_PW}"})
    h = Harness()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(InstallStateError, match="GUARD_PG_PW"):
        await h.installer().install(spec.ref)
    # Nothing persisted: no env key, no manifest entry, no attribution row.
    assert h.cm._env == {}
    assert h.cm._manifest.get("mcp", []) == []
    assert h.store.rows == {}


async def test_combined_transaction_lands_values_and_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${GUARD_PG_PW}"})
    h = Harness()
    h.registry.resolved = make_resolved(spec)
    result = await h.installer().install(spec.ref, env={"GUARD_PG_PW": "s3cret"}, secret_keys=["GUARD_PG_PW"])
    # The value landed in the env store, marked secret; the entry landed in the manifest.
    assert h.cm._env["GUARD_PG_PW"] == "s3cret"
    assert h.cm._env["TAI_ENV_SECRET_KEYS"] == "GUARD_PG_PW"
    assert h.cm._manifest["mcp"][0]["title"] == "postgres"
    assert h.store.rows[spec.ref].version == "1.0.0"
    assert any("mounted MCP server 'postgres'" in note for note in result["notes"])


async def test_env_accepted_when_all_markers_carry_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # A marker with a DEFAULT (``!ENV ${VAR:x}``) is not REQUIRED, so required_env is empty —
    # but the spec still ACCEPTS env (a marker is present). Supplying env for that var must be
    # accepted and WRITTEN through the accepts_env combined route, not rejected as "no env".
    from tai42_kit.plugins import accepts_env, required_env_for_spec

    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${GUARD_PG_PW:x}"})
    assert list(required_env_for_spec(spec)) == []  # the marker's default makes it non-required
    assert accepts_env(spec) is True  # yet the spec accepts env (a marker is present)

    h = Harness()
    h.registry.resolved = make_resolved(spec)
    result = await h.installer().install(spec.ref, env={"GUARD_PG_PW": "supplied"}, secret_keys=["GUARD_PG_PW"])
    # The supplied value landed via the combined env+manifest route (accepts_env), marked
    # secret, alongside the mcp entry.
    assert h.cm._env["GUARD_PG_PW"] == "supplied"
    assert h.cm._env["TAI_ENV_SECRET_KEYS"] == "GUARD_PG_PW"
    assert h.cm._manifest["mcp"][0]["title"] == "postgres"
    assert any("mounted MCP server 'postgres'" in note for note in result["notes"])


async def test_manifest_persist_failure_leaves_no_orphan_env_write(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${GUARD_PG_PW}"})
    h = Harness()
    h.registry.resolved = make_resolved(spec)
    h.svc.fail_persist = True
    with pytest.raises(OrphanEnvWriteError):
        await h.installer().install(spec.ref, env={"GUARD_PG_PW": "s3cret"})
    # The pipeline leaves the env standing, but the installer-unwind deletes
    # exactly the keys THIS install wrote — no orphan remains at the installer layer.
    assert h.cm._env == {}
    assert h.store.rows == {}


async def test_persist_failure_restores_prior_secret_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    # A secret_keys-bearing install whose manifest-persist FAILS must leave NO orphan of
    # either kind: the secret VALUE key deleted AND the TAI_ENV_SECRET_KEYS marks var back
    # to its PRIOR value — restoring an operator's OTHER marks, never leaving this install's
    # appended key standing.
    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${GUARD_PG_PW}"})
    h = Harness(env={"TAI_ENV_SECRET_KEYS": "OTHER_KEY"})
    h.registry.resolved = make_resolved(spec)
    h.svc.fail_persist = True
    with pytest.raises(OrphanEnvWriteError):
        await h.installer().install(spec.ref, env={"GUARD_PG_PW": "s3cret"}, secret_keys=["GUARD_PG_PW"])
    # The secret value key is gone; the marks var is back to exactly the operator's prior
    # mark (this install's appended GUARD_PG_PW is not left behind).
    assert h.cm._env == {"TAI_ENV_SECRET_KEYS": "OTHER_KEY"}
    assert h.store.rows == {}


async def test_persist_failure_deletes_marks_var_absent_before(monkeypatch: pytest.MonkeyPatch) -> None:
    # When TAI_ENV_SECRET_KEYS did not exist before the install, a persist failure deletes
    # the marks var this install created outright — no orphan marks var left standing.
    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${GUARD_PG_PW}"})
    h = Harness()
    h.registry.resolved = make_resolved(spec)
    h.svc.fail_persist = True
    with pytest.raises(OrphanEnvWriteError):
        await h.installer().install(spec.ref, env={"GUARD_PG_PW": "s3cret"}, secret_keys=["GUARD_PG_PW"])
    assert h.cm._env == {}
    assert h.store.rows == {}


async def test_persist_failure_restores_prior_store_value_not_this_installs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Over-revert / data-loss guard: a supplied env key that ALREADY EXISTS in the config
    # store (value present, NOT in os.environ) and that this install merely OVERWRITES must,
    # on a manifest-persist failure, be restored to its PRIOR store value — never blind-
    # deleted (which would lose the operator's pre-existing value) and never left at this
    # install's value.
    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${GUARD_PG_PW}"})
    h = Harness(env={"GUARD_PG_PW": "prior-value"})
    h.registry.resolved = make_resolved(spec)
    h.svc.fail_persist = True
    with pytest.raises(OrphanEnvWriteError):
        await h.installer().install(spec.ref, env={"GUARD_PG_PW": "new-value"})
    # The operator's prior store value survives the unwind — not "", not "new-value".
    assert h.cm._env == {"GUARD_PG_PW": "prior-value"}
    assert h.store.rows == {}


async def test_pre_persist_refusal_writes_nothing_and_broadcasts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both required var KEYS are supplied (so the early presence pre-check passes), but one
    # carries an EMPTY value — the store drops empties, so the pipeline's dangling-marker
    # validation refuses it BEFORE anything persists. The unwind must not fire a fleet
    # broadcast on that no-op: nothing landed in the store, so the env-revert diff is empty
    # and no apply_env_change runs.
    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${GUARD_PG_PW}", "PGUSER": "!ENV ${GUARD_PG_USER}"})
    h = Harness()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(InstallEnvError, match="GUARD_PG_USER"):
        await h.installer().install(
            spec.ref, env={"GUARD_PG_PW": "s3cret", "GUARD_PG_USER": ""}, secret_keys=["GUARD_PG_PW"]
        )
    # Byte-identical store (nothing written) AND zero env-change broadcasts.
    assert h.cm._env == {}
    assert h.cm._manifest.get("mcp", []) == []
    assert h.store.rows == {}
    assert h.svc.env_change_calls == 0


async def test_non_mcp_spec_with_env_is_a_loud_input_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # env / secret_keys are meaningless for a spec that declares no install-time env (a
    # plain tool spec accepts_env == False); supplying either is a loud InstallEnvError
    # before any state changes.
    spec = make_spec()  # a plain tool-kind spec, no install-time env
    h = Harness()
    h.registry.resolved = make_resolved(spec)
    with pytest.raises(InstallEnvError, match="declares no install-time env"):
        await h.installer().install(spec.ref, env={"SOME_VAR": "v"})
    with pytest.raises(InstallEnvError, match="declares no install-time env"):
        await h.installer().install(spec.ref, secret_keys=["SOME_VAR"])
    assert h.cm._env == {}
    assert h.store.rows == {}


async def test_upgrade_adding_a_required_var_is_refused_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    old = _mcp_spec(version="1.0.0")  # no markers
    new = _mcp_spec(version="2.0.0", env={"PGPASSWORD": "!ENV ${GUARD_PG_PW}"})
    h = Harness(manifest={"mcp": [{"title": "postgres", "config": {"command": "pg-mcp-server"}}]})
    h.store.preload(old)
    h.registry.resolved = make_resolved(new, version="2.0.0")
    # The early env pre-check fires (the new required var is neither supplied nor stored).
    with pytest.raises(InstallStateError, match="GUARD_PG_PW"):
        await h.installer().update(old.ref)
    # Refused before persist: the manifest still carries the OLD entry, the row is unchanged.
    assert h.store.rows[old.ref].version == "1.0.0"


async def test_uninstall_leaves_env_and_surfaces_orphans(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${GUARD_PG_PW}"})
    entry = {"title": "postgres", "config": {"command": "pg-mcp-server", "env": {"PGPASSWORD": "!ENV ${GUARD_PG_PW}"}}}
    h = Harness(manifest={"mcp": [entry]}, env={"GUARD_PG_PW": "s3cret"})
    h.store.preload(spec)
    result = await h.installer().uninstall(spec.ref)
    # The entry is gone from the manifest, but the stored secret is LEFT untouched.
    assert h.cm._manifest["mcp"] == []
    assert h.cm._env["GUARD_PG_PW"] == "s3cret"
    assert h.store.rows == {}
    assert any("GUARD_PG_PW" in note for note in result["notes"])


def test_sanity_apply_provides_entry_validates() -> None:
    # The written entry mounts as a valid TaiMCPConfig would (a default-bearing marker
    # resolves without an env), so the manifest schema accepts it.
    import os

    from pyaml_env import parse_config
    from tai42_kit.utils.data import dump_manifest

    spec = _mcp_spec(env={"PGPASSWORD": "!ENV ${GUARD_PG_PW:x}"})
    manifest: dict = {}
    apply_provides(manifest, spec)
    resolved = parse_config(data=dump_manifest(cast("Any", manifest))) or {}
    Manifest.model_validate(resolved)
    assert os.environ
