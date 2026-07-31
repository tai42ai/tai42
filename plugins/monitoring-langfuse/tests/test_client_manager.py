"""Client-manager coverage: build/eviction lifecycle, project registry, and the
source / environment marker."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest
from tai42_contract.monitoring import ProjectConfig

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager


def _cfg(public_key="pk", source="tai", timeout_seconds=30):
    return ProjectConfig(
        public_key=public_key,
        secret_key="sk",
        host="http://localhost",
        source=source,
        timeout_seconds=timeout_seconds,
    )


@pytest.fixture
def fake_langfuse(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace the SDK client constructor; returns the captured build kwargs."""
    constructed: list[dict] = []
    monkeypatch.setattr(
        "tai42_monitoring_langfuse.client_manager.Langfuse",
        lambda **kw: constructed.append(kw) or MagicMock(name="client"),
    )
    return constructed


def test_requires_at_least_one_project():
    with pytest.raises(ValueError, match="at least one project"):
        LangfuseClientManager([], "pk")


def test_default_public_key_must_be_configured():
    with pytest.raises(ValueError, match="not among the configured projects"):
        LangfuseClientManager([_cfg()], "other")


def test_build_client_passes_credentials_and_environment(fake_langfuse):
    mgr = LangfuseClientManager([_cfg(source="shared-team", timeout_seconds=7)], "pk")
    mgr._ensure_built()
    assert len(fake_langfuse) == 1
    built = fake_langfuse[0]
    assert built["public_key"] == "pk"
    assert built["secret_key"] == "sk"
    assert built["host"] == "http://localhost"
    assert built["timeout"] == 7
    assert built["environment"] == "shared-team"


def test_ensure_built_builds_once(fake_langfuse):
    mgr = LangfuseClientManager([_cfg()], "pk")
    mgr._ensure_built()
    mgr._ensure_built()
    assert len(fake_langfuse) == 1


def test_ensure_built_rechecks_under_lock(fake_langfuse):
    # Double-checked build: a builder finishing while this caller waits on the
    # lock must not be rebuilt over. The lock stand-in marks built on acquire.
    mgr = LangfuseClientManager([_cfg()], "pk")

    class _FlipLock:
        def __enter__(self) -> None:
            mgr._built = True

        def __exit__(self, *exc: object) -> bool:
            return False

    mgr._lock = _FlipLock()  # type: ignore[assignment]
    mgr._ensure_built()
    assert fake_langfuse == []  # the winner's clients stand; nothing rebuilt


def test_active_client_returns_configured_client(fake_langfuse):
    mgr = LangfuseClientManager([_cfg()], "pk")
    client = mgr.active_client()
    assert client is mgr._clients["pk"]


def test_active_client_falls_back_to_shared_registry_for_foreign_key(fake_langfuse, monkeypatch):
    foreign = MagicMock(name="foreign-client")
    monkeypatch.setattr("tai42_monitoring_langfuse.client_manager.get_client", lambda public_key: foreign)
    mgr = LangfuseClientManager([_cfg()], "pk")
    monkeypatch.setattr(mgr, "resolve_public_key", lambda: "pk-foreign")
    assert mgr.active_client() is foreign


def test_add_project_is_idempotent_and_builds_immediately_when_built(fake_langfuse):
    mgr = LangfuseClientManager([_cfg()], "pk")
    mgr._ensure_built()
    mgr.add_project(_cfg(public_key="pk-b"))
    assert "pk-b" in mgr._clients  # built immediately, selectable via scope()
    assert len(fake_langfuse) == 2
    mgr.add_project(_cfg(public_key="pk-b"))
    assert len(fake_langfuse) == 2  # idempotent


def test_add_project_before_build_defers_construction(fake_langfuse):
    mgr = LangfuseClientManager([_cfg()], "pk")
    mgr.add_project(_cfg(public_key="pk-b"))
    assert fake_langfuse == []
    mgr._ensure_built()
    assert len(fake_langfuse) == 2


def test_active_source_returns_active_projects_source():
    mgr = LangfuseClientManager([_cfg(source="tai")], "pk")
    assert mgr.active_source() == "tai"


def test_active_source_raises_for_unconfigured_active_key(monkeypatch):
    mgr = LangfuseClientManager([_cfg()], "pk")
    # Simulate an active key the manager never registered.
    monkeypatch.setattr(mgr, "resolve_public_key", lambda: "unknown-key")
    with pytest.raises(ValueError, match="unknown-key"):
        mgr.active_source()


def test_read_timeout_seconds_uses_project_or_default(monkeypatch):
    mgr = LangfuseClientManager([_cfg(timeout_seconds=7)], "pk")
    assert mgr.read_timeout_seconds() == 7
    monkeypatch.setattr(mgr, "resolve_public_key", lambda: "pk-foreign")
    assert mgr.read_timeout_seconds() == 30  # foreign project: default read timeout


def test_flush_noop_before_build_then_flushes_every_client(fake_langfuse):
    mgr = LangfuseClientManager([_cfg(), _cfg(public_key="pk-b")], "pk")
    mgr.flush()  # nothing built, nothing constructed just to flush
    assert fake_langfuse == []
    mgr._ensure_built()
    mgr.flush()
    for client in mgr._clients.values():
        cast("MagicMock", client).flush.assert_called_once_with()


def test_shutdown_fully_evicts_and_marks_for_rebuild(fake_langfuse, monkeypatch):
    evict = MagicMock()
    monkeypatch.setattr("tai42_monitoring_langfuse.client_manager.evict_all_clients", evict)

    mgr = LangfuseClientManager([_cfg()], "pk")
    mgr._ensure_built()
    assert mgr._built is True
    assert len(fake_langfuse) == 1

    mgr.shutdown()
    evict.assert_called_once_with()
    assert mgr._built is False
    assert mgr._clients == {}

    # Next use rebuilds a fresh client (the forked-child path).
    mgr._ensure_built()
    assert mgr._built is True
    assert len(fake_langfuse) == 2
