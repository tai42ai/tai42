"""Bind a recording stub app to the ``tai42_app`` handle before the plugin is
imported, and build a REAL ``LocalSandbox`` over a temp workspace root.

The plugin registers its provider through ``tai42_app`` at import time; binding the
stub here (at collection time, before any test imports the plugin) captures that
registration. Every runtime fixture drives a genuine host-subprocess sandbox — a
host subprocess is always available, so this is a real engine, not a fake.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tai42_contract.app import tai42_app


class StubSandboxes:
    def __init__(self) -> None:
        self.registered: type | None = None

    def register_sandbox(self, cls: type) -> type:
        self.registered = cls
        return cls


class StubApp:
    def __init__(self) -> None:
        self.sandboxes = StubSandboxes()


_stub_app = StubApp()
tai42_app.bind(_stub_app)

# Imported AFTER the bind so the import-time registration lands in the stub.
import tai42_sandbox_local  # noqa: E402


@pytest.fixture
def stub_app() -> StubApp:
    return _stub_app


@pytest.fixture
def local_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``SANDBOX_LOCAL_ROOT`` at a fresh temp dir for the test, and reset the
    settings cache so the provider reads it."""
    from tai42_kit.settings import reset_all_settings

    root = tmp_path / "sbx-root"
    root.mkdir()
    monkeypatch.setenv("SANDBOX_LOCAL_ROOT", str(root))
    reset_all_settings()
    yield root
    reset_all_settings()


@pytest.fixture
def sandbox(local_root: Path) -> tai42_sandbox_local.LocalSandbox:
    """A live ``LocalSandbox`` with a permissive policy bound (so only a PROVIDER
    inability, never the policy chokepoint, rejects a spec)."""
    from tai42_kit.sandbox import permissive_policy

    sb = tai42_sandbox_local.LocalSandbox()
    sb.bind_policy(permissive_policy())
    return sb
