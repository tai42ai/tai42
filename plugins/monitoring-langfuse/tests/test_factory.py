"""Factory: env-or-explicit project construction, loud failure without creds."""

from __future__ import annotations

import pytest
from tai42_contract.monitoring import Monitoring, ProjectConfig

from tai42_monitoring_langfuse import LangfuseMonitoring, build_langfuse_backend


def _cfg(public_key: str, source: str = "tai") -> ProjectConfig:
    return ProjectConfig(public_key=public_key, secret_key=f"sk-{public_key}", host="http://lf", source=source)


def test_langfuse_without_creds_raises(monkeypatch):
    # Selecting Langfuse but leaving credentials unset is a misconfiguration:
    # it must fail loudly, not silently degrade to no-op.
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="LANGFUSE_PUBLIC_KEY"):
        build_langfuse_backend()


def test_langfuse_with_creds_builds_backend(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost")
    backend = build_langfuse_backend()
    assert isinstance(backend, LangfuseMonitoring)
    assert isinstance(backend, Monitoring)  # runtime-checkable contract protocol
    assert backend._manager._default_public_key == "pk"


def test_explicit_projects_default_to_first(monkeypatch):
    backend = build_langfuse_backend(projects=[_cfg("pk-a"), _cfg("pk-b")])
    assert isinstance(backend, LangfuseMonitoring)
    assert backend._manager._default_public_key == "pk-a"


def test_explicit_projects_with_default_key():
    backend = build_langfuse_backend(projects=[_cfg("pk-a"), _cfg("pk-b")], default_public_key="pk-b")
    assert isinstance(backend, LangfuseMonitoring)
    assert backend._manager._default_public_key == "pk-b"


def test_add_project_registers_for_scope():
    backend = LangfuseMonitoring(projects=[_cfg("pk-a")], default_public_key="pk-a")
    backend.add_project(_cfg("pk-b"))
    assert "pk-b" in backend._manager._projects
    # Idempotent.
    backend.add_project(_cfg("pk-b"))
    assert list(backend._manager._projects).count("pk-b") == 1
