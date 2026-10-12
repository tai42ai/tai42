"""Worker lifespan resource teardown and the ``app_context`` startup-failure abort."""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from tai42_contract.connectors.models import AuthHealthState
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_skeleton.manifest import Manifest

from ...connectors import mcp_fakes
from ._credential_app import BUDGET, OTHER_CONNECTION_ID, AppHarness, bound, roomy_budgets
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


def test_the_lifespan_binds_the_refresh_owner_to_its_serving_loop_and_closes_it_at_shutdown():
    from tai42_skeleton.app import instance
    from tai42_skeleton.connectors.runtime import refresh_owner

    seen: dict[str, bool] = {}

    async def run():
        async with instance.app.app_context(Manifest.model_validate({})):
            seen["live"] = refresh_owner.is_live()
            seen["serving_loop"] = refresh_owner._loop is asyncio.get_running_loop()
        seen["live_after"] = refresh_owner.is_live()

    asyncio.run(run())
    assert seen == {"live": True, "serving_loop": True, "live_after": False}


# -- credentials brought up to date after boot's listing -----------------------------------------
#
# Boot lists before the refresh owner is live; right after binding it the lifespan refreshes the
# credentials of the servers that demanded one and re-probes those entries once. See ``_credential_app``.


def _spy_reprobe(monkeypatch) -> list[set[str]]:
    from tai42_skeleton.app.lifecycle.mcp_reprobe import McpReprobeMixin

    snapshots: list[set[str]] = []
    original = McpReprobeMixin._probe_and_apply_failed

    async def spy(self, snapshot):
        snapshots.append(set(snapshot))
        return await original(self, snapshot)

    monkeypatch.setattr(McpReprobeMixin, "_probe_and_apply_failed", spy)
    return snapshots


def test_a_boot_with_an_expired_credential_ends_bound_before_the_app_yields(monkeypatch, path):
    from tai42_skeleton.app import instance

    harness = AppHarness(monkeypatch, path)
    path.connect(expires_in=-5)
    path.mcp.reset(mcp_fakes.bearer_only("at-new"))
    path.token_endpoint()
    snapshots = _spy_reprobe(monkeypatch)

    async def run():
        async with instance.app.app_context(harness.manifest()):
            assert bound(path.entry.title)

    asyncio.run(run())
    assert snapshots == [{path.entry.title}]
    assert path.token_requests == 1


def test_an_entry_down_for_another_reason_is_not_reprobed_by_the_boot_step(monkeypatch, path):
    from tai42_skeleton.app import instance

    harness = AppHarness(monkeypatch, path)
    down = TaiMCPConfig(title="down", config=MCPConfig(type="http", url="http://127.0.0.1:9/mcp"))
    harness.entries.append(down)
    path.connect(expires_in=-5)
    path.mcp.reset(mcp_fakes.bearer_only("at-new"))
    path.token_endpoint()
    snapshots = _spy_reprobe(monkeypatch)

    async def run():
        async with instance.app.app_context(harness.manifest()):
            assert bound(path.entry.title)
            assert down.title in instance.app._failed_mcps

    asyncio.run(run())
    assert snapshots == [{path.entry.title}]


def test_a_token_endpoint_slower_than_the_budget_holds_boot_at_most_two_budgets(monkeypatch, path):
    from tai42_skeleton.app import instance
    from tai42_skeleton.app.lifecycle.mcp_reprobe import McpReprobeMixin

    harness = AppHarness(monkeypatch, path)
    path.connect(expires_in=-5)
    path.mcp.reset(mcp_fakes.bearer_only("at-new"))
    path.token_endpoint(delay=BUDGET * 3)
    original = McpReprobeMixin._bring_boot_credentials_up_to_date
    spent: list[float] = []

    async def timed(self):
        started = time.monotonic()
        await original(self)
        spent.append(time.monotonic() - started)

    monkeypatch.setattr(McpReprobeMixin, "_bring_boot_credentials_up_to_date", timed)

    async def run():
        async with instance.app.app_context(harness.manifest()):
            assert not bound(path.entry.title)
            deadline = time.monotonic() + 10
            while path.stored().refresh_token.get_secret_value() != "rt-rotated":
                assert time.monotonic() < deadline, "the refresh did not complete"
                await asyncio.sleep(0.02)
            roomy_budgets(monkeypatch)
            await instance.app._probe_and_apply_failed({path.entry.title: path.entry})
            assert bound(path.entry.title)

    asyncio.run(run())
    assert len(spent) == 1
    assert spent[0] < 2 * BUDGET + 0.5


def test_an_entry_that_fails_the_server_check_is_not_resolved_by_the_boot_step(monkeypatch, path):
    from tai42_skeleton.app import instance

    harness = AppHarness(monkeypatch, path)
    harness.entries = []
    forged = harness.forged()
    path.connect(expires_in=-5)
    path.mcp.reset(mcp_fakes.bearer_only("at-new"))
    path.token_endpoint()

    async def run():
        async with instance.app.app_context(harness.manifest()):
            assert forged.title in instance.app._failed_mcps

    asyncio.run(run())
    assert path.loads == 0
    assert path.token_requests == 0


@pytest.mark.parametrize("outcome", ["store error", "reconnect required"])
def test_a_failure_for_one_entry_at_boot_is_counted_and_stops_nothing(monkeypatch, path, caplog, outcome):
    from tai42_skeleton.app import instance

    harness = AppHarness(monkeypatch, path)
    other = harness.other_connection(
        ConnectionError("store unreachable") if outcome == "store error" else AuthHealthState.RECONNECT_REQUIRED
    )
    path.connect(expires_in=-5)
    path.mcp.reset(mcp_fakes.bearer_only("at-new"))
    path.token_endpoint()

    async def run():
        async with instance.app.app_context(harness.manifest()):
            assert bound(path.entry.title)
            assert other.title in instance.app._failed_mcps

    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.connectors.runtime.credentials_step"):
        asyncio.run(run())
    step_warnings = [
        r
        for r in caplog.records
        if r.name == "tai42_skeleton.connectors.runtime.credentials_step" and r.levelno == logging.WARNING
    ]
    if outcome == "store error":
        assert any(OTHER_CONNECTION_ID in r.getMessage() for r in step_warnings)
    else:
        assert step_warnings == []
