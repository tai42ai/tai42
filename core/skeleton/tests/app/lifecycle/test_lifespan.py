"""Worker lifespan resource teardown, the ``app_context`` startup-failure abort and the state its exit leaves."""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import mcp
import pytest
from tai42_contract.connectors.models import AuthHealthState
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_skeleton.manifest import Manifest

from ...connectors import mcp_fakes
from ._credential_app import BUDGET, OTHER_CONNECTION_ID, AppHarness, bound, roomy_budgets
from ._doubles import _Mixin


def _teardown_mixin(monkeypatch):
    """A mixin with the kit's loop-bound release and the monitoring stubbed in the lifecycle
    module namespace, so ``_teardown_resources`` is observable without real resources.

    ``steps`` notes each step as it runs, in order."""
    m = _Mixin()
    steps: list[str] = []
    release = AsyncMock(side_effect=lambda: steps.append("loop-bound kit resources"))
    writer = MagicMock()
    writer.flush.side_effect = lambda: steps.append("monitoring flush")
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.release_loop_bound_resources", release)
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.get_monitoring", lambda: MagicMock(writer=writer))
    return m, steps, release, writer


def test_teardown_resources_releases_the_kits_loop_bound_resources_then_flushes(monkeypatch, caplog):
    m, steps, release, writer = _teardown_mixin(monkeypatch)
    with caplog.at_level(logging.INFO, logger="tai42_skeleton.app.lifecycle.lifespan"):
        asyncio.run(m._teardown_resources())
    release.assert_awaited_once_with()
    writer.flush.assert_called_once_with()
    assert steps == ["loop-bound kit resources", "monitoring flush"]
    assert [r.getMessage() for r in caplog.records] == ["shutdown teardown finished: every step ran"]


def test_teardown_resources_flushes_when_the_kit_release_raises_then_raises_the_one_failure(monkeypatch):
    # A failing kit release must not skip the flush; the failure is raised once the last step ran.
    m, _steps, release, writer = _teardown_mixin(monkeypatch)
    release.side_effect = RuntimeError("checkpoint boom")

    with pytest.raises(RuntimeError, match="checkpoint boom"):
        asyncio.run(m._teardown_resources())

    writer.flush.assert_called_once_with()


def test_teardown_resources_raises_several_failures_together(monkeypatch):
    m, _steps, release, writer = _teardown_mixin(monkeypatch)
    release.side_effect = RuntimeError("checkpoint boom")
    writer.flush.side_effect = OSError("flush boom")

    with pytest.raises(ExceptionGroup, match="shutdown teardown failed") as ei:
        asyncio.run(m._teardown_resources())

    assert [type(e) for e in ei.value.exceptions] == [RuntimeError, OSError]


def test_teardown_resources_lets_a_cancellation_through_and_logs_the_failures_before_it(monkeypatch, caplog):
    # A cancellation is no step failure: it propagates as itself, a failure collected before it
    # is logged rather than lost, and a WARNING names the step it cut short and the steps that
    # never ran.
    m, _steps, release, writer = _teardown_mixin(monkeypatch)
    release.side_effect = RuntimeError("pool boom")
    writer.flush.side_effect = asyncio.CancelledError

    with caplog.at_level(logging.INFO), pytest.raises(asyncio.CancelledError):
        asyncio.run(m._teardown_resources())

    logged = [r.exc_info[1] for r in caplog.records if r.levelno == logging.ERROR and r.exc_info]
    assert [str(e) for e in logged] == ["pool boom"]
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warned == [
        "shutdown teardown cut short by CancelledError during monitoring flush, which did not finish; never ran: none"
    ]


def test_app_context_startup_handler_failure_raises(monkeypatch):
    # The startup path runs the handlers with raise_on_error: a failed startup
    # handler must abort the boot loudly, never yield a healthy-looking
    # half-initialized app.
    m, *_ = _teardown_mixin(monkeypatch)

    async def start(manifest):
        return None

    monkeypatch.setattr(m, "start", start)

    @m._on_startup
    async def boom():
        raise RuntimeError("startup boom")

    async def run():
        async with m.app_context(Manifest.model_validate({})):
            pass  # pragma: no cover — startup fails before the body runs

    with pytest.raises(RuntimeError, match=r"lifecycle handlers failed.*startup boom"):
        asyncio.run(run())


def test_the_lifespan_exit_leaves_no_state_of_the_generation_it_served(monkeypatch):
    from tai42_skeleton.app import instance

    tool = mcp.types.Tool(name="send", inputSchema={"type": "object", "properties": {}})
    monkeypatch.setattr(instance.app, "_probe_mcp", AsyncMock(return_value=[tool]))
    manifest = Manifest.model_validate(
        {"mcp": [{"title": "acme", "include": [], "config": {"type": "http", "url": "http://x/mcp"}}]}
    )

    async def run():
        async with instance.app.app_context(manifest):
            assert instance.app._mcp_bound_tools == {"acme": {"acme_send"}}
            instance.app._failed_mcps["other"] = {"status": "unavailable", "category": "error", "message": "m"}

    asyncio.run(run())

    # A later reader in this process sees no bound title, listing, record or manifest of that generation.
    assert instance.app._manifest is None
    assert instance.app._mcp_bound_tools == {}
    assert instance.app._mcp_listed_tools == {}
    assert instance.app._failed_mcps == {}


def test_a_later_app_context_on_the_same_loop_leases_a_pooled_client_anew_and_its_exit_releases_it():
    from tai42_kit.clients import PooledClient

    from tai42_skeleton.app import instance

    events: list[str] = []

    class _ProbeClient(PooledClient[str]):
        async def _create(self, **kwargs: object) -> str:
            return str(kwargs["context"])

        async def _close(self, client: str) -> None:
            events.append(f"closed {client}")

    async def run():
        for context in ("first", "second"):
            async with (
                instance.app.app_context(Manifest.model_validate({})),
                _ProbeClient().current(context=context) as client,
            ):
                events.append(f"leased {client}")
            events.append(f"exited {context}")

    asyncio.run(run())

    assert events == [
        "leased first",
        "closed first",
        "exited first",
        "leased second",
        "closed second",
        "exited second",
    ]


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
