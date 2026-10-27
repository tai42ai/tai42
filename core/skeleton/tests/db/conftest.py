"""Shared fixtures for the chain-discovery suite.

Migrate's discovery unites the installed scan with the chains of the plugins the persisted
manifest loads. These suites exercise the installed scan, so they configure an EMPTY
manifest here (no loaded modules); a test reading a manifest file overrides it.
"""

from __future__ import annotations

import pytest


class _EmptyManifestManager:
    """A config-manager stand-in whose manifest carries no loaded modules."""

    def read_manifest(self) -> dict:
        return {}


@pytest.fixture(autouse=True)
def _empty_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.config import ConfigManagerFactory

    monkeypatch.setattr(ConfigManagerFactory, "create", lambda: _EmptyManifestManager())
