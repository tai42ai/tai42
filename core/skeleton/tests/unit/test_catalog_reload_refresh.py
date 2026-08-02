"""Reload-handler rerun on the in-place ``update()`` path.

``update()`` re-runs the registered reload handlers under ``raise_on_error``, so a
config reload re-applies every reload-time side effect and a failing handler fails
the op loudly rather than leaving the worker silently behind.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
from tai42_skeleton.manifest import Manifest


class _Mixin(TaiMCPLifecycleMixin):
    """Concrete-enough subclass to exercise update()'s handler re-run without
    an event server, network, or the full app: start() is stubbed to skip the
    re-init and there are no live tools to drop."""

    def __init__(self):
        super().__init__()
        self.started_with = None

    def _mcp_tools(self, config, tools):  # abstract in the mixin
        pass

    def start(self, manifest):
        self.started_with = manifest


def test_update_reruns_reload_handlers() -> None:
    mixin = _Mixin()
    ran = []

    @mixin._on_reload
    async def _reload_marker() -> None:
        ran.append("reload")

    manifest = Manifest()
    mixin._update(manifest)

    assert mixin.started_with is manifest
    assert ran == ["reload"]


def test_update_raises_when_reload_handler_fails() -> None:
    # raise_on_error on the update path: a failed reload handler must fail the op
    # loudly, never leave the worker silently behind.
    mixin = _Mixin()

    @mixin._on_reload
    async def _boom() -> None:
        raise RuntimeError("reload blew up")

    with pytest.raises(RuntimeError, match="lifecycle handlers failed"):
        mixin._update(Manifest())
