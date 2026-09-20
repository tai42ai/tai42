"""Shared fakes and helpers for the :class:`~tai42_skeleton.config.service.ConfigService`
pipeline oracles — an in-memory config store (the transactional seams), a fake reload admin,
a recording worker bus, the service wiring helper, the env-toggle helpers, and the oauth /
none connector descriptors the stickiness tests drive."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, cast

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, OpOutcome, WorkerIdentity, WorkerKind, WorkerResult
from tai42_skeleton.config.service import ConfigService

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


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
    """A store that re-runs the mutator (as an external store's optimistic-concurrency retry does)
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


def _oauth_descriptor(provider_id: str = "acme") -> dict[str, Any]:
    return {
        "id": provider_id,
        "display_name": provider_id.title(),
        "icon_url": f"https://example.com/{provider_id}.png",
        "kind": "oauth",
        "origin": "system",
        "category": "productivity",
        "oauth": {"authorize": "https://auth.example.com/authorize", "token": "https://auth.example.com/token"},
        "client_id_env": f"{provider_id.upper()}_CLIENT_ID",
        "client_secret_env": f"{provider_id.upper()}_CLIENT_SECRET",
        "sub_services": {
            "main": {
                "id": "main",
                "display_name": "Main",
                "scopes": ["read"],
                "mcp_server": {"type": "http", "url": "https://mcp.example.com/mcp"},
            }
        },
    }


def _none_descriptor(provider_id: str = "acme") -> dict[str, Any]:
    return {
        "id": provider_id,
        "display_name": provider_id.title(),
        "icon_url": f"https://example.com/{provider_id}.png",
        "kind": "none",
        "origin": "system",
        "category": "productivity",
        "sub_services": {
            "main": {
                "id": "main",
                "display_name": "Main",
                "mcp_server": {"type": "http", "url": "https://mcp.example.com/mcp"},
            }
        },
    }
