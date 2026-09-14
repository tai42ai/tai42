"""Unit oracles for :class:`~tai42_skeleton.config.service.ConfigService` — the env doors:
``apply_env_change`` (an env override) and ``apply_env_and_change`` (the combined
env-write + manifest-mutate seam), plus connector-secret stickiness across a dropped oauth
connector.

The combined-op tests assert env-first/manifest-second ordering, the no-rollback orphan
contract, the external-store 409 replay writing the env exactly once, and the process-wide
lock serializing a concurrent secret-marks append; the stickiness tests assert a leaving
``client_secret_env`` name is folded into the stored marks so its value stays masked.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from tai42_skeleton.config.service import OrphanEnvWriteError

from .fake_pipeline import (
    FakeConfigStore,
    RetryingConfigStore,
    _no_bus,
    _none_descriptor,
    _oauth_descriptor,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
    the shape ``set_mcp_secret_env`` produces (re-runnable, external-store-409-replay-safe)."""

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


async def test_apply_env_and_change_external_store_409_replay_writes_env_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # An external store's optimistic-concurrency retry re-runs the manifest mutator (RetryingConfigStore
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
# Connector-secret stickiness across removal
# ---------------------------------------------------------------------------


async def test_apply_change_dropping_oauth_connector_persists_its_secret_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"connectors": [_oauth_descriptor("acme")]}, env={})
    service, _admin, _bus = _service(store)

    def drop(document: dict[str, Any]) -> None:
        document["connectors"] = []

    await service.apply_change(drop)

    # The connector is gone, but its client_secret_env name is now in the stored marks so
    # the orphaned value keeps its mask (the value itself is never deleted).
    assert store.manifest["connectors"] == []
    assert "ACME_CLIENT_SECRET" in store.env["TAI_ENV_SECRET_KEYS"].split(",")


async def test_update_to_none_kind_keeps_the_dropped_secret_name_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An oauth->none update keeps the connector id but drops its client_secret_env NAME; the
    # name must still be persisted into the marks (a set difference of NAMES, not of ids).
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"connectors": [_oauth_descriptor("acme")]}, env={})
    service, _admin, _bus = _service(store)

    def to_none(document: dict[str, Any]) -> None:
        document["connectors"] = [_none_descriptor("acme")]

    await service.apply_change(to_none)

    assert store.manifest["connectors"][0]["kind"] == "none"
    assert "ACME_CLIENT_SECRET" in store.env["TAI_ENV_SECRET_KEYS"].split(",")


async def test_apply_change_not_dropping_a_connector_writes_no_marks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A manifest change that drops NO oauth connector never touches the marks var (no env
    # write) — the plain apply_change path.
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, _admin, _bus = _service(store)

    def add(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    await service.apply_change(add)
    assert store.env_writes == []
    assert "TAI_ENV_SECRET_KEYS" not in store.env


async def test_apply_replace_dropping_connector_persists_mark_and_deletes_omitted_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hand `manifest replace` that drops an oauth connector AND omits another top-level
    # section: the leaving secret name is persisted, and the omitted section is DELETED (the
    # delegated replace-as-mutator is clear()+update(), never a merge).
    _with_bus(monkeypatch)
    store = FakeConfigStore(
        manifest={"connectors": [_oauth_descriptor("acme")], "mcp": [{"title": "srv", "config": {"url": "http://x"}}]},
        env={},
    )
    service, _admin, _bus = _service(store)

    # The replacement carries neither the connector nor the mcp section.
    await service.apply_replace({"user_tools": []})

    assert "connectors" not in store.manifest or store.manifest["connectors"] == []
    assert "mcp" not in store.manifest  # the omitted section was deleted, not merged
    assert "ACME_CLIENT_SECRET" in store.env["TAI_ENV_SECRET_KEYS"].split(",")


async def test_leaving_mark_written_only_when_it_grows_the_stored_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The leaving name is ALREADY in the stored marks, so dropping the connector must NOT
    # rewrite the marks var (no growth).
    _with_bus(monkeypatch)
    store = FakeConfigStore(
        manifest={"connectors": [_oauth_descriptor("acme")]},
        env={"TAI_ENV_SECRET_KEYS": "ACME_CLIENT_SECRET"},
    )
    service, _admin, _bus = _service(store)

    def drop(document: dict[str, Any]) -> None:
        document["connectors"] = []

    await service.apply_change(drop)
    # No env write carried the marks var (the set did not grow); the value stands.
    assert all("TAI_ENV_SECRET_KEYS" not in write for write in store.env_writes)
    assert store.env["TAI_ENV_SECRET_KEYS"] == "ACME_CLIENT_SECRET"
