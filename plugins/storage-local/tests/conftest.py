"""Bind a minimal ``tai42_app`` impl before the backend is imported.

``tai42_storage_local.storage`` registers ``LocalStorage`` via ``tai42_app`` at
import time, so the handle must be bound first. The stub exposes only the
``storage.register_storage`` seam the backend needs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.storage import Storage


class _StorageFacet:
    def __init__(self) -> None:
        self.provider: Storage | None = None

    def register_storage(
        self, cls: type[Storage] | None = None
    ) -> Callable[[type[Storage]], type[Storage]] | type[Storage]:
        if cls is None:

            def decorator(inner: type[Storage]) -> type[Storage]:
                self.provider = inner()
                return inner

            return decorator
        self.provider = cls()
        return cls


class _StubApp:
    def __init__(self) -> None:
        self.storage = _StorageFacet()


_stub_app = _StubApp()
tai42_app.bind(_stub_app)


@pytest.fixture
def registered_provider() -> Storage | None:
    """The provider the backend registered as an import side-effect."""
    return _stub_app.storage.provider


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the env-driven settings at a temp dir and clear the settings cache."""
    from tai42_storage_local.settings import storage_settings

    monkeypatch.setenv("STORAGE_LOCAL_ROOT_PATH", str(tmp_path))
    monkeypatch.setenv("STORAGE_LOCAL_CREATE_DIRS", "true")
    storage_settings.cache_clear()  # type: ignore[attr-defined]
    yield tmp_path
    storage_settings.cache_clear()  # type: ignore[attr-defined]
