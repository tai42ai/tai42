"""Unit oracles for :class:`~tai42_skeleton.config.service.ConfigService` — the single
manifest-mutation pipeline.

Each test drives the service against a fake config store (the transactional seams),
a fake reload admin, and a fake worker bus, asserting the pipeline's ordering and
its failure discipline:

* validation runs on the RESOLVED projection and rejects before any persist;
* the mutator is pure / re-runnable;
* ``apply_replace`` validates BEFORE it persists;
* a local reload that fails after the persist landed still broadcasts, then re-raises
  with the fleet report attached;
* an unconfirmed origin is a loud ERROR log but a returned success;
* the bus-unreachable shape is a returned success too;
* the backend-needs-bus invariant rejects both directions.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import TYPE_CHECKING, Any, cast

import pytest
from pyaml_env import parse_config
from pydantic import ValidationError
from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.data import dump_manifest

from tai42_skeleton.app import instance
from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, OpOutcome, WorkerIdentity, WorkerKind, WorkerResult
from tai42_skeleton.config.secret_seal import ResolvedSecretError
from tai42_skeleton.config.service import ConfigService, OrphanEnvWriteError
from tai42_skeleton.operations._broadcast import FleetBroadcastError, apply_response

from .._fakes.bus import FakeBus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConfigStore:
    """A config manager whose transactional seams persist into an in-memory document.

    ``mutate_manifest`` runs the mutator on a fresh copy of the stored PRESERVED
    document and persists it only if the mutator returns without raising — so an
    aborting mutator leaves the store untouched, exactly like the real transaction.
    """

    def __init__(self, *, manifest: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> None:
        self.manifest: dict[str, Any] = manifest if manifest is not None else {}
        self.env: dict[str, str] = env if env is not None else {}
        self.persisted: list[dict[str, Any]] = []
        self.env_writes: list[dict[str, str]] = []

    def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        document = copy.deepcopy(self.manifest)
        mutator(document)  # a raise here propagates before any persist
        self.manifest = document
        self.persisted.append(copy.deepcopy(document))
        return document

    def replace_manifest(self, document: dict[str, Any]) -> dict[str, Any]:
        self.manifest = copy.deepcopy(document)
        self.persisted.append(copy.deepcopy(document))
        return self.manifest

    def write_env(self, config: dict[str, str]) -> None:
        self.env_writes.append(dict(config))
        self.env = {**self.env, **config}

    def replace_env(self, config: dict[str, str]) -> None:
        # Whole-map replace: a key absent from ``config`` is deleted; empties filtered.
        self.env_writes.append(dict(config))
        self.env = {key: value for key, value in config.items() if value != ""}

    def read_env(self) -> dict[str, str]:
        return dict(self.env)

    def read_manifest_preserved(self) -> dict[str, Any]:
        return copy.deepcopy(self.manifest)


class RetryingConfigStore(FakeConfigStore):
    """A store that re-runs the mutator (as the k8s optimistic-concurrency retry does)
    before persisting, so a test can prove the guarded mutator is re-runnable."""

    def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        # First attempt is discarded (a simulated conflict); the second is persisted.
        mutator(copy.deepcopy(self.manifest))
        return super().mutate_manifest(mutator)


class FakeReloadAdmin:
    """``during_reload`` runs inside the local reload, so a test can age the fleet the
    way a slow reimporting reload does — the window the op-start membership snapshot
    exists to cover."""

    def __init__(
        self,
        *,
        result: dict[str, Any] | None = None,
        raise_reload: Exception | None = None,
        during_reload: Callable[[], None] | None = None,
    ) -> None:
        self._result = result if result is not None else {"status": "ok", "env_keys": 0}
        self._raise = raise_reload
        self._during_reload = during_reload
        self.calls = 0

    def reload_config(self) -> dict[str, Any]:
        self.calls += 1
        if self._during_reload is not None:
            self._during_reload()
        if self._raise is not None:
            raise self._raise
        return self._result


class RecordingBus:
    """A worker bus that records each publish and returns a crafted fleet report.

    ``remote_outcome`` shapes the report of a configured remote origin (``applied`` for
    a converged fleet, ``missing`` for an unconfirmed one); ``reachable=False`` returns
    the bus-unreachable shape (no origin list, only an error). ``publish_error`` makes
    ``publish`` RAISE that exception after recording the call — a non-transport
    broadcast fault (e.g. a redis ``ResponseError``) that the bus does not fold into a
    returned bus-unreachable report.

    ``live`` is the mutable census behind ``expected_at_start``: dropping a name from it
    mid-apply is a worker's presence fading, and ``census_error`` makes the op-start
    census itself raise. Each publish's snapshot is recorded in
    ``expected_at_start_calls``, separately from the ``publish_calls`` triples."""

    def __init__(
        self,
        *,
        remotes: list[str] | None = None,
        remote_outcome: OpOutcome = OpOutcome.applied,
        reachable: bool = True,
        error: str | None = None,
        publish_error: Exception | None = None,
        census_error: Exception | None = None,
    ) -> None:
        self.identity = WorkerIdentity(name="serve-test", kind=WorkerKind.serve, pid=1, generation=1)
        self._remotes = remotes or []
        self._remote_outcome = remote_outcome
        self._reachable = reachable
        self._error = error
        self._publish_error = publish_error
        self._census_error = census_error
        self.live = set(self._remotes)
        self.publish_calls: list[tuple[dict[str, Any], list[str] | None, LocalApplyResult | None]] = []
        self.expected_at_start_calls: list[dict[str, int] | None] = []

    async def expected_at_start(self) -> dict[str, int]:
        if self._census_error is not None:
            raise self._census_error
        return dict.fromkeys(sorted(self.live), 1)

    async def publish(
        self,
        op: dict[str, Any],
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        self.publish_calls.append((op, targets, local))
        self.expected_at_start_calls.append(expected_at_start)
        if self._publish_error is not None:
            raise self._publish_error
        if not self._reachable:
            return FleetResult(op=op["op"], reachable=False, error=self._error)
        results: list[WorkerResult] = []
        if local is not None:
            results.append(
                WorkerResult(name=self.identity.name, outcome=local.outcome, payload=local.payload, error=local.error)
            )
        for remote in self._remotes:
            results.append(WorkerResult(name=remote, outcome=self._remote_outcome, detail="crafted"))
        return FleetResult(op=op["op"], results=results)


def _service(
    store: FakeConfigStore, *, admin: FakeReloadAdmin | None = None, bus: RecordingBus | None = None
) -> tuple[ConfigService, FakeReloadAdmin, RecordingBus]:
    admin = admin or FakeReloadAdmin()
    bus = bus or RecordingBus()
    service = ConfigService(config_manager=store, admin=admin, bus=cast("Any", bus))
    return service, admin, bus


@pytest.fixture(autouse=True)
def _reset_settings_after() -> Iterator[None]:
    yield
    reset_all_settings()


def _with_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()


def _no_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()


# ---------------------------------------------------------------------------
# apply_change
# ---------------------------------------------------------------------------


async def test_apply_change_mutates_validates_persists_reloads_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [*document.get("mcp", []), {"title": "srv", "config": {"url": "http://x"}}]

    result = await service.apply_change(add_server)

    # Persisted the mutated document exactly once, then reloaded locally, then
    # broadcast the reload to the WHOLE fleet with the local result as `local`.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert admin.calls == 1
    assert len(bus.publish_calls) == 1
    op, targets, local = bus.publish_calls[0]
    assert op == {"op": "reload_config"}
    assert targets is None
    assert local is not None
    assert local.outcome == OpOutcome.applied
    # ApplyResult carries the persisted document, the local reload result, and the report.
    assert result.document == {"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}
    assert result.local == {"status": "ok", "env_keys": 0}
    assert result.fleet.op == "reload_config"
    assert result.fleet.ok is True


async def test_apply_result_fanout_is_the_apply_response_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    # fleet_fanout reads the process bus origin to decide local-only vs fleet, so drive
    # the pipeline through that same bus (installed as instance.app.bus) — the value the
    # connector writers thread must equal exactly what apply_response embeds.
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = FakeBus(origin="serve-test", remotes=["serve-w1"])
    monkeypatch.setattr(instance.app, "_bus", bus)
    service, _admin, _bus = _service(store, bus=cast("Any", bus))

    result = await service.apply_replace({"mcp": []})

    assert result.fanout == apply_response(result)["fanout"]
    assert result.fanout["mode"] == "fleet"
    assert {r["name"] for r in result.fanout["results"]} == {"serve-test", "serve-w1"}


async def test_apply_change_invalid_manifest_raises_with_nothing_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    def break_manifest(document: dict[str, Any]) -> None:
        document["tools"] = "not-a-list"  # fails Manifest schema validation

    with pytest.raises(ValidationError):
        await service.apply_change(break_manifest)

    # Validation rejected inside the transaction: nothing persisted, no reload, no broadcast.
    assert store.persisted == []
    assert store.manifest == {"mcp": []}
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_change_mutator_rerun_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    # The store re-runs the mutator (a simulated concurrency conflict) before it
    # persists; a pure mutator yields the same document and persists once.
    store = RetryingConfigStore(manifest={"mcp": []})
    service, admin, _bus = _service(store)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [*document.get("mcp", []), {"title": "srv", "config": {"url": "http://x"}}]

    result = await service.apply_change(add_server)

    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert result.document == {"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}
    assert admin.calls == 1


# ---------------------------------------------------------------------------
# apply_replace
# ---------------------------------------------------------------------------


async def test_apply_replace_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": [{"title": "old", "config": {"url": "http://old"}}]})
    service, admin, bus = _service(store)

    document = {"mcp": [{"title": "new", "config": {"url": "http://new"}}]}
    result = await service.apply_replace(document)

    assert store.manifest == document
    assert admin.calls == 1
    assert bus.publish_calls[0][0] == {"op": "reload_config"}
    assert bus.publish_calls[0][1] is None
    assert result.document == document


async def test_apply_replace_validates_before_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    with pytest.raises(ValidationError):
        await service.apply_replace({"tools": "not-a-list"})

    # A replace has no mutator to abort, so validation must precede the persist.
    assert store.persisted == []
    assert store.manifest == {"mcp": []}
    assert admin.calls == 0
    assert bus.publish_calls == []


# ---------------------------------------------------------------------------
# Secret seal — a resolved !ENV secret never bakes to disk (decision 19)
# ---------------------------------------------------------------------------

# A manifest's mcp section is only ever read through the RESOLVED view, so the natural
# round-trip (read the resolved view → edit → post it back) hands the pipeline resolved
# secret values. The seal retags them back to the operator's !ENV marker before persist,
# and refuses a stranded resolved secret with no marker origin.
_TOKEN = "super-secret-token-value"


def _resolved(document: dict[str, Any]) -> dict[str, Any]:
    """The RESOLVED view a client reads through ``GET /api/manifest`` — ``!ENV``
    markers materialized against the current env, exactly as the live manifest
    exposes them."""
    return cast("dict[str, Any]", parse_config(data=dump_manifest(cast("Any", document))) or {})


def _marker_manifest() -> dict[str, Any]:
    """A manifest whose one mcp server carries an ``!ENV`` auth header marker."""
    return {
        "mcp": [
            {"title": "srv", "config": {"url": "http://x", "headers": {"Authorization": "!ENV ${TOKEN}"}}},
        ]
    }


async def test_apply_change_resolved_round_trip_restores_env_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # The exact set_mcp_config round-trip: the client reads the RESOLVED mcp section,
    # edits it, and posts it back as document["mcp"] = <resolved list>. The seal must
    # restore the operator's !ENV marker so no resolved token bakes to disk.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    store = FakeConfigStore(manifest=_marker_manifest())
    service, _admin, _bus = _service(store)
    resolved = _resolved(store.manifest)
    # The client read the resolved view: its Authorization is the plaintext token.
    assert resolved["mcp"][0]["config"]["headers"]["Authorization"] == _TOKEN

    def post_resolved(document: dict[str, Any]) -> None:
        # set_mcp_config's mutator: wholesale-replace mcp with the client-supplied
        # (resolved) list.
        document["mcp"] = copy.deepcopy(resolved["mcp"])

    result = await service.apply_change(post_resolved)

    # The persisted document restored the !ENV marker; the resolved token never landed.
    assert result.document is not None
    assert result.document["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${TOKEN}"
    assert _TOKEN not in str(store.persisted)


async def test_apply_replace_resolved_round_trip_restores_env_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # A whole-document replace carrying a resolved secret where the current doc has a
    # marker is retagged, so the marker persists.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    store = FakeConfigStore(manifest=_marker_manifest())
    service, _admin, _bus = _service(store)

    replacement = copy.deepcopy(_resolved(store.manifest))
    result = await service.apply_replace(replacement)

    assert result.document is not None
    assert result.document["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${TOKEN}"
    assert _TOKEN not in str(store.persisted)


async def test_apply_replace_stranded_resolved_secret_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # A replacement carrying a resolved secret whose entry has NO marker origin (its
    # identity was renamed away) is a stranded plaintext secret — refused loudly.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    store = FakeConfigStore(manifest=_marker_manifest())
    service, admin, bus = _service(store)

    stranded = copy.deepcopy(_resolved(store.manifest))
    stranded["mcp"][0]["title"] = "renamed"  # no marker origin now; the token is plaintext

    with pytest.raises(ResolvedSecretError) as exc:
        await service.apply_replace(stranded)

    # ValueError-mappable to a 400 by the operations layer, and it names the offending path.
    assert isinstance(exc.value, ValueError)
    assert "mcp[0]" in str(exc.value)
    # Refused before any persist.
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_change_pure_marker_mutator_persists_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pure in-place mutator whose leaves already carry !ENV markers (a connector-style
    # append of a preserved-marker entry) is a retag no-op and passes the leak net with
    # no false rejection — the markers persist verbatim.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    monkeypatch.setenv("OTHER", "other-secret-value")
    store = FakeConfigStore(manifest={"mcp": [{"title": "srv", "config": {"env": {"KEY": "!ENV ${TOKEN}"}}}]})
    service, _admin, _bus = _service(store)

    def append_marker_entry(document: dict[str, Any]) -> None:
        document["mcp"].append({"title": "srv2", "config": {"env": {"KEY2": "!ENV ${OTHER}"}}})

    result = await service.apply_change(append_marker_entry)

    assert result.document is not None
    # Both the untouched and the appended entries keep their !ENV markers verbatim.
    assert result.document["mcp"][0]["config"]["env"]["KEY"] == "!ENV ${TOKEN}"
    assert result.document["mcp"][1]["config"]["env"]["KEY2"] == "!ENV ${OTHER}"
    assert _TOKEN not in str(store.persisted)


# ---------------------------------------------------------------------------
# apply_env_change
# ---------------------------------------------------------------------------


async def test_apply_env_change_writes_reloads_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, bus = _service(store)

    result = await service.apply_env_change({"NEW_KEY": "v"})

    assert store.env_writes == [{"NEW_KEY": "v"}]
    assert store.env == {"EXISTING": "1", "NEW_KEY": "v"}
    assert admin.calls == 1
    assert bus.publish_calls[0] == ({"op": "reload_config"}, None, bus.publish_calls[0][2])
    # An env change touches no manifest document.
    assert result.document is None
    assert result.local == {"status": "ok", "env_keys": 0}


# ---------------------------------------------------------------------------
# apply_env_and_change (combined env-write + manifest-mutate)
# ---------------------------------------------------------------------------


def _secret_marker_mutator(var: str) -> Callable[[dict[str, Any]], None]:
    """A pure mutator that writes an ``!ENV ${var}`` marker into a fresh MCP entry —
    the shape ``set_mcp_secret_env`` produces (re-runnable, k8s-409-replay-safe)."""

    def mutator(document: dict[str, Any]) -> None:
        document["mcp"] = [
            {"title": "gh", "config": {"url": "https://x", "headers": {"Authorization": f"!ENV ${{{var}}}"}}}
        ]

    return mutator


def _prepare(
    changes: dict[str, str], mutator: Callable[[dict[str, Any]], None]
) -> Callable[[dict[str, str]], Awaitable[tuple[dict[str, str], Callable[[dict[str, Any]], None]]]]:
    """Wrap fixed ``(changes, mutator)`` as the async ``prepare`` callback
    :meth:`ConfigService.apply_env_and_change` now takes — the real op derives these from the
    lock-held stored-env read; a test not exercising derivation returns them verbatim."""

    async def _p(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        return changes, mutator

    return _p


async def test_apply_env_and_change_writes_env_and_mutates_manifest_consistently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, _bus = _service(store)

    result = await service.apply_env_and_change(_prepare({"GH": "the-secret"}, _secret_marker_mutator("GH")))

    # Env write + manifest mutate both landed, consistently.
    assert store.env == {"EXISTING": "1", "GH": "the-secret"}
    assert store.manifest["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${GH}"
    # The persisted manifest keeps the MARKER (no resolved secret bakes to disk).
    assert store.persisted[-1]["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${GH}"
    assert admin.calls == 1
    assert result.document is not None


async def test_apply_env_and_change_manifest_failure_leaves_orphan_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # partial-failure contract: env-FIRST/manifest-SECOND; a manifest
    # persist failure AFTER the env write does NOT roll the env back — the env write STANDS as
    # an inert, re-runnable orphan — and the op raises loudly (OrphanEnvWriteError) NAMING the
    # orphan env key AND the manifest pointer, stating the env write stands / re-run.
    _no_bus(monkeypatch)

    class FailingMutateStore(FakeConfigStore):
        def mutate_manifest(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
            raise RuntimeError("manifest persist boom")

    store = FailingMutateStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, admin, _bus = _service(store)

    with pytest.raises(OrphanEnvWriteError) as excinfo:
        await service.apply_env_and_change(
            _prepare({"GH": "the-secret"}, _secret_marker_mutator("GH")),
            manifest_pointer="mcp/0/config/headers/Authorization",
        )

    message = str(excinfo.value)
    assert "GH" in message  # names the now-orphan env key
    assert "mcp/0/config/headers/Authorization" in message  # names the manifest pointer
    assert "stands" in message.lower()  # env write stands
    assert "re-run" in message.lower()  # re-runnable
    # The original persist failure is chained, not swallowed.
    assert isinstance(excinfo.value.__cause__, RuntimeError)

    # NO rollback: the env write STANDS — the orphan key REMAINS in the store (a single
    # write_env, no compensating replace_env), and nothing persisted / reloaded.
    assert store.env == {"EXISTING": "1", "GH": "the-secret"}
    assert store.env_writes == [{"GH": "the-secret"}]  # only the write_env; no rollback restore
    assert store.persisted == []
    assert admin.calls == 0


async def test_apply_env_and_change_k8s_409_replay_writes_env_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # The k8s optimistic-concurrency retry re-runs the manifest mutator (RetryingConfigStore
    # discards a first attempt). The env write is OUTSIDE that replayed span, so it happens
    # exactly once — env + manifest stay consistent on a 409 replay, no double env write.
    _no_bus(monkeypatch)
    store = RetryingConfigStore(manifest={"mcp": []}, env={})
    service, _admin, _bus = _service(store)

    await service.apply_env_and_change(_prepare({"GH": "the-secret"}, _secret_marker_mutator("GH")))

    assert store.env_writes == [{"GH": "the-secret"}]  # single env write despite the manifest replay
    assert store.env == {"GH": "the-secret"}
    assert store.manifest["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${GH}"


async def test_apply_env_and_change_refuses_x_band_env_key_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={"EXISTING": "1"})
    service, _admin, _bus = _service(store)

    with pytest.raises(ValueError, match="TAI_RUN_MODE"):
        await service.apply_env_and_change(
            _prepare({"GH": "the-secret", "TAI_RUN_MODE": "spoof"}, _secret_marker_mutator("GH"))
        )

    # X-band refused up front — neither store was touched.
    assert store.env == {"EXISTING": "1"}
    assert store.env_writes == []
    assert store.persisted == []


async def test_apply_env_and_change_refuses_dangling_marker_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The mutator writes an `!ENV ${MISSING}` marker but the env changes do not supply
    # MISSING → dangling, refused before any write (naming the var).
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, _admin, _bus = _service(store)

    with pytest.raises(ValueError, match="MISSING"):
        await service.apply_env_and_change(_prepare({"OTHER": "v"}, _secret_marker_mutator("MISSING")))

    assert store.env_writes == []
    assert store.persisted == []


def _marks_appending_prepare(
    key: str,
) -> Callable[[dict[str, str]], Awaitable[tuple[dict[str, str], Callable[[dict[str, Any]], None]]]]:
    """A ``prepare`` that mirrors the read→append→write hazard: it reads the stored marks,
    YIELDS (``await asyncio.sleep(0)``) to force a concurrent op to try to interleave, THEN
    appends its own key. Under the env-write lock the yield cannot let the other op read stale
    marks; without it, both would read the same marks and the second write would lose the first."""

    async def _p(stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        existing = [m for m in stored.get("TAI_ENV_SECRET_KEYS", "").split(",") if m]
        await asyncio.sleep(0)  # the interleave point the lock must cover
        marks = list(dict.fromkeys([*existing, key]))
        changes = {key: f"secret-{key}", "TAI_ENV_SECRET_KEYS": ",".join(marks)}
        return changes, _secret_marker_mutator(key)

    return _p


async def test_apply_env_and_change_lock_serializes_concurrent_marks_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two concurrent combined ops each APPEND their secret mark. Each prepare reads the
    # stored marks, YIELDS to force interleave, then appends — so without serialization both
    # read the same marks and the second write clobbers the first (a lost append). The
    # process-wide (CLASS-level) env-write lock must serialize the read→write span so BOTH
    # marks survive. Two separate ConfigService instances (as ``from_app`` builds per call)
    # over ONE shared store prove the lock is shared across instances, not per-instance.
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service_a, admin_a, _bus_a = _service(store)
    service_b, admin_b, _bus_b = _service(store)

    await asyncio.gather(
        service_a.apply_env_and_change(_marks_appending_prepare("KEY_A")),
        service_b.apply_env_and_change(_marks_appending_prepare("KEY_B")),
    )

    marks = store.env["TAI_ENV_SECRET_KEYS"].split(",")
    assert "KEY_A" in marks, f"KEY_A's mark was lost to the concurrent append: {marks}"
    assert "KEY_B" in marks, f"KEY_B's mark was lost to the concurrent append: {marks}"
    # Both secret values landed too, and each op ran its own reload (lock released before it).
    assert store.env["KEY_A"] == "secret-KEY_A"
    assert store.env["KEY_B"] == "secret-KEY_B"
    assert admin_a.calls == 1
    assert admin_b.calls == 1


# ---------------------------------------------------------------------------
# Failure discipline
# ---------------------------------------------------------------------------


async def test_local_reload_failure_after_persist_still_broadcasts_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    admin = FakeReloadAdmin(raise_reload=RuntimeError("reload boom"))
    service, _admin, bus = _service(store, admin=admin)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    # The persist landed; the failed local reload does NOT strand the fleet.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert len(bus.publish_calls) == 1
    _op, _targets, local = bus.publish_calls[0]
    assert local is not None
    assert local.outcome == OpOutcome.failed
    # The fleet report the broadcast produced rides the raised error.
    assert exc.value.report.op == "reload_config"


async def test_broadcast_raise_after_persist_becomes_fleet_broadcast_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    # A non-transport broadcast fault the bus does NOT fold into a returned
    # bus-unreachable report (e.g. a redis ResponseError, or a malformed presence key
    # the census cannot parse) — it raises RAW out of publish AFTER the persist landed.
    bus = RecordingBus(publish_error=RuntimeError("ResponseError: WRONGTYPE"))
    service, admin, _bus = _service(store, bus=bus)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    # The raw broadcast error was wrapped as FleetBroadcastError, never propagated raw,
    # and rides as the cause.
    assert isinstance(exc.value.__cause__, RuntimeError)
    # The persist DID land — the committed mutation is in the store — and the local
    # reload ran before the broadcast raised.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert admin.calls == 1
    # The error carries the honest bus-unreachable report (no origin list, only error).
    assert exc.value.report.op == "reload_config"
    assert exc.value.report.reachable is False
    assert exc.value.report.results == []
    assert "ResponseError" in (exc.value.report.error or "")


async def test_apply_replace_broadcast_raise_after_persist_becomes_fleet_broadcast_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": [{"title": "old", "config": {"url": "http://old"}}]})
    bus = RecordingBus(publish_error=RuntimeError("ResponseError: WRONGTYPE"))
    service, admin, _bus = _service(store, bus=bus)

    document = {"mcp": [{"title": "new", "config": {"url": "http://new"}}]}
    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_replace(document)

    # apply_replace honors the same post-persist contract: the replace committed, then
    # the raw broadcast error surfaced as FleetBroadcastError with the unreachable report.
    assert store.manifest == document
    assert admin.calls == 1
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert exc.value.report.reachable is False


# ---------------------------------------------------------------------------
# Expected membership is pinned to op start, not to publish time
#
# publish censuses when it is called — after the local reload. A worker whose presence
# fades across a reimporting reload would drop off that census and the report would read
# converged without it, so the pipeline snapshots membership BEFORE the reload.
# ---------------------------------------------------------------------------


async def test_expected_membership_is_censused_before_the_local_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-w1"])
    # The sibling is live when the op begins and its presence fades DURING the reload.
    admin = FakeReloadAdmin(during_reload=lambda: bus.live.discard("serve-w1"))
    service, _admin, _bus = _service(store, admin=admin, bus=bus)

    await service.apply_replace({"mcp": [{"title": "new", "config": {"url": "http://new"}}]})

    # Read first, so the sibling is still owed a confirmation; a snapshot taken after the
    # reload would be empty and the op would converge without it.
    assert bus.live == set()
    assert bus.expected_at_start_calls == [{"serve-w1": 1}]


async def test_op_start_census_failure_is_loud_but_never_aborts_the_reload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The snapshot rides on top of publish's own census, which degrades a dead bus to an
    # honest unreachable report — so a census blip must not turn a survivable post-persist
    # reload into a raise. Loud, because the op then runs on the membership the snapshot
    # exists to correct.
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-w1"], census_error=ConnectionError("bus census unreachable"))
    service, admin, _bus = _service(store, bus=bus)

    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.operations._broadcast"):
        result = await service.apply_replace({"mcp": [{"title": "new", "config": {"url": "http://new"}}]})

    assert admin.calls == 1
    assert result.fleet.ok
    assert bus.expected_at_start_calls == [None]
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name == "tai42_skeleton.operations._broadcast"
    ]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert "op-start census" in warnings[0].getMessage()


async def test_broadcast_raise_with_local_reload_failure_is_single_fleet_broadcast_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    admin = FakeReloadAdmin(raise_reload=RuntimeError("reload boom"))
    bus = RecordingBus(publish_error=RuntimeError("ResponseError: WRONGTYPE"))
    service, _admin, _bus = _service(store, admin=admin, bus=bus)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    # Both the local reload AND the broadcast failed after the persist landed — a SINGLE
    # FleetBroadcastError surfaces, carrying the broadcast error as cause and an
    # unreachable report whose error notes the local reload failure too.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert exc.value.report.reachable is False
    assert "ResponseError" in (exc.value.report.error or "")
    assert "local reload also failed" in (exc.value.report.error or "")


async def test_unconfirmed_origin_logs_error_but_returns_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-w1"], remote_outcome=OpOutcome.missing)
    service, _admin, _bus = _service(store, bus=bus)

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.operations._broadcast"):
        result = await service.apply_replace({"mcp": []})

    # Persist + local reload landed, so the call SUCCEEDS; the unconfirmed origin is a
    # loud ERROR log and an explicit non-applied entry in the report.
    assert result.fleet.ok is False
    assert {r.name: r.outcome for r in result.fleet.results}["serve-w1"] == OpOutcome.missing
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert any("did not fully converge" in record.message for record in caplog.records)


async def test_bus_unreachable_returns_success_with_unreachable_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(reachable=False, error="ConnectionError: bus down")
    service, admin, _bus = _service(store, bus=bus)

    result = await service.apply_replace({"mcp": []})

    # Persist + local reload landed, so the call SUCCEEDS even though the transport was
    # down: the honest bus-unreachable shape (no origin list, only an error) rides through.
    assert admin.calls == 1
    assert result.fleet.reachable is False
    assert result.fleet.error == "ConnectionError: bus down"
    assert result.fleet.results == []


# ---------------------------------------------------------------------------
# backend-needs-bus invariant — both directions
# ---------------------------------------------------------------------------


async def test_manifest_change_adding_backend_without_bus_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={})
    service, admin, bus = _service(store)

    def add_backend(document: dict[str, Any]) -> None:
        document["backend_module"] = "myapp.backend"

    with pytest.raises(BackendNeedsBusError, match="TAI_BUS_REDIS_URL"):
        await service.apply_change(add_backend)

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_manifest_change_adding_backend_with_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={})
    service, admin, _bus = _service(store)

    def add_backend(document: dict[str, Any]) -> None:
        document["backend_module"] = "myapp.backend"

    result = await service.apply_change(add_backend)

    assert store.manifest == {"backend_module": "myapp.backend"}
    assert admin.calls == 1
    assert result.document == {"backend_module": "myapp.backend"}


async def test_env_change_materializing_backend_without_bus_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    # The manifest's backend module is an !ENV marker; the env change supplies the
    # value that materializes it — with no bus, the invariant rejects it.
    monkeypatch.delenv("TAI_BACKEND", raising=False)
    store = FakeConfigStore(manifest={"backend_module": "!ENV ${TAI_BACKEND}"})
    service, _admin, bus = _service(store)

    with pytest.raises(BackendNeedsBusError, match="TAI_BUS_REDIS_URL"):
        await service.apply_env_change({"TAI_BACKEND": "myapp.backend"})

    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_env_change_removing_bus_while_backend_present_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    # A static backend is registered and the bus is configured only through the stored
    # env; the change empties the bus var — after it, a backend would run with no bus.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, _admin, bus = _service(store)

    with pytest.raises(BackendNeedsBusError, match="TAI_BUS_REDIS_URL"):
        await service.apply_env_change({"TAI_BUS_REDIS_URL": ""})

    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_env_change_keeping_bus_with_backend_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, admin, _bus = _service(store)

    result = await service.apply_env_change({"SOME_KEY": "v"})

    assert store.env_writes == [{"SOME_KEY": "v"}]
    assert admin.calls == 1
    assert result.document is None


async def test_env_change_with_backend_and_default_namespace_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bus configured ONLY through the shared TAI_DEFAULT_REDIS_URL (no
    # TAI_BUS_REDIS_URL) resolves as ENABLED through BusSettings, so an unrelated env
    # edit on a backend deployment is NOT falsely rejected (a raw TAI_BUS_REDIS_URL
    # read would have missed the default and rejected every edit).
    _no_bus(monkeypatch)
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    try:
        store = FakeConfigStore(manifest={"backend_module": "myapp.backend"}, env={})
        service, admin, _bus = _service(store)

        result = await service.apply_env_change({"SOME_KEY": "v"})

        assert store.env_writes == [{"SOME_KEY": "v"}]
        assert admin.calls == 1
        assert result.document is None
    finally:
        reset_all_settings()
