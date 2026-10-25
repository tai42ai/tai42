"""A fresh storage prefix-mark registry on the app's current serving core, for one test."""

from __future__ import annotations

import pytest

from tai42_skeleton.app import instance
from tai42_skeleton.storage.prefix_marks import PrefixMarkRegistry


def fresh_prefix_marks(monkeypatch: pytest.MonkeyPatch) -> PrefixMarkRegistry:
    """Install an empty registry on the serving core and return it; the test's undo restores the old one."""
    registry = PrefixMarkRegistry()
    monkeypatch.setattr(instance.app._serving_core, "_prefix_mark_registry", registry)
    return registry
