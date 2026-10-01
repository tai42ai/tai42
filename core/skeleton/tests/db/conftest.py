"""Shared fixtures for the chain-discovery suite.

Discovery resolves the effective manifest to see the chains the deployment LOADS; a
real deployment always has a manifest (boot reads it with no fallback). These suites
exercise the store and prefix sources, so they configure an EMPTY manifest here (no
loaded modules); the manifest-source tests override it with their own document, and the
absent-manifest tests override it to signal a missing file.
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
