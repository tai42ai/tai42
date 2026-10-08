"""Worker lifespan resource teardown and the ``app_context`` startup-failure abort."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tai42_skeleton.manifest import Manifest

from ._doubles import _Mixin


def _teardown_mixin(monkeypatch):
    """A mixin with a fake clients facet plus the kit release and monitoring stubbed in the
    lifecycle module namespace, so ``_teardown_resources`` is observable without real pools."""
    m = _Mixin()
    clients = MagicMock(shutdown_clients=AsyncMock())
    m.clients = clients
    release = AsyncMock()
    writer = MagicMock()
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.release_loop_bound_resources", release)
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.get_monitoring", lambda: MagicMock(writer=writer))
    return m, clients, release, writer


def test_teardown_resources_releases_every_epoch_and_flushes(monkeypatch):
    m, clients, release, writer = _teardown_mixin(monkeypatch)
    asyncio.run(m._teardown_resources())
    clients.shutdown_clients.assert_awaited_once()
    release.assert_awaited_once_with(all_epochs=True)
    writer.flush.assert_called_once_with()


def test_teardown_resources_runs_every_step_then_raises_group(monkeypatch):
    # One failing step must not skip the rest; collected failures surface as an
    # ExceptionGroup rather than being swallowed.
    m, clients, release, writer = _teardown_mixin(monkeypatch)
    release.side_effect = RuntimeError("checkpoint boom")

    with pytest.raises(ExceptionGroup) as ei:
        asyncio.run(m._teardown_resources())

    # Every other step still ran despite the release failure.
    clients.shutdown_clients.assert_awaited_once()
    writer.flush.assert_called_once_with()
    assert "shutdown teardown failed" in str(ei.value)
    assert any(isinstance(e, RuntimeError) for e in ei.value.exceptions)


def test_app_context_startup_handler_failure_raises(monkeypatch):
    # The startup path runs the handlers with raise_on_error: a failed startup
    # handler must abort the boot loudly, never yield a healthy-looking
    # half-initialized app.
    m, *_ = _teardown_mixin(monkeypatch)
    monkeypatch.setattr(m, "start", lambda manifest: None)

    @m._on_startup
    async def boom():
        raise RuntimeError("startup boom")

    async def run():
        async with m.app_context(Manifest.model_validate({})):
            pass  # pragma: no cover — startup fails before the body runs

    with pytest.raises(RuntimeError, match=r"lifecycle handlers failed.*startup boom"):
        asyncio.run(run())
