"""Soft config reload: env refresh into the process and dropping keys removed at source."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest

from ...connectors import mcp_fakes
from ._credential_app import BUDGET, DOORS, OTHER_CONNECTION_ID, AppHarness, bound, roomy_budgets


def test_reload_config_refreshes_env_and_reinitializes(monkeypatch):
    import os

    from tai42_kit.clients.base import current_client_epoch

    from tai42_skeleton.app.reload_gate import reload_gate

    captured_env = {"NEW_KEY": "v1", "OTHER": "v2"}

    async def run():
        async with app.app_context(Manifest.model_validate({})):
            monkeypatch.setattr(app.config.config_manager, "read_env", lambda: captured_env)
            monkeypatch.setattr(app.config.config_manager, "read_manifest", dict)

            before = current_client_epoch()
            # Driven off the serving loop, as production does (the reload-gate worker
            # thread): a fresh epoch is built under the refreshed env and swapped in.
            out = await reload_gate.run(app.admin.reload_config, reimports=True)
            assert out == {"status": "ok", "env_keys": 2, "awaiting_respawn": []}
            # Env refreshed into the process environment.
            assert os.environ["NEW_KEY"] == "v1"
            assert os.environ["OTHER"] == "v2"
            # Reinitialized: the client epoch advanced (a fresh serving epoch swapped in).
            assert current_client_epoch() == before + 1

    try:
        asyncio.run(run())
    finally:
        os.environ.pop("NEW_KEY", None)
        os.environ.pop("OTHER", None)


def test_reload_config_drops_env_keys_removed_from_source(monkeypatch):
    import os

    from tai42_skeleton.app.reload_gate import reload_gate

    async def run():
        async with app.app_context(Manifest.model_validate({})):
            monkeypatch.setattr(app.config.config_manager, "read_manifest", dict)

            monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {"K1_RC": "a", "K2_RC": "b"})
            await reload_gate.run(app.admin.reload_config, reimports=True)
            assert os.environ["K1_RC"] == "a"
            assert os.environ["K2_RC"] == "b"

            # K2 removed from the source env: the next reload must drop it, not
            # leave it lingering as stale config.
            monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {"K1_RC": "a"})
            await reload_gate.run(app.admin.reload_config, reimports=True)
            assert os.environ["K1_RC"] == "a"
            assert "K2_RC" not in os.environ

    try:
        asyncio.run(run())
    finally:
        os.environ.pop("K1_RC", None)
        os.environ.pop("K2_RC", None)


# -- credentials brought up to date before the build ---------------------------------------------
#
# A managed OAuth entry whose server demands a credential, booted and then reloaded through each of
# the two doors that rebuild an epoch; see ``_credential_app``.


@pytest.mark.parametrize("door", DOORS)
def test_an_expired_credential_is_refreshed_before_the_build_and_the_entry_binds(monkeypatch, path, door):
    harness = AppHarness(monkeypatch, path)
    path.connect()
    path.mcp.reset(mcp_fakes.bearer_only("at-old", "at-new"))
    path.token_endpoint()

    async def run():
        async with app.app_context(harness.manifest()):
            assert bound(path.entry.title)
            path.connect(expires_in=-5)
            await harness.reload(door)
            assert bound(path.entry.title)

    asyncio.run(run())
    assert path.token_requests == 1
    assert path.stored().refresh_token.get_secret_value() == "rt-rotated"


async def _until_rotated(path, timeout: float = 60.0) -> None:
    """Wait, without blocking the serving loop the refresh runs on, until the rotated token is stored."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stored = path.stored()
        if stored.refresh_token is not None and stored.refresh_token.get_secret_value() == "rt-rotated":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the refresh did not complete and store the rotated token")


@pytest.mark.parametrize("door", DOORS)
def test_a_token_endpoint_slower_than_the_budget_holds_the_reload_one_budget(monkeypatch, path, door):
    from tai42_skeleton.app import epoch

    harness = AppHarness(monkeypatch, path)
    path.connect()
    path.mcp.reset(mcp_fakes.bearer_only("at-old", "at-new"))
    path.token_endpoint(delay=BUDGET * 3)
    original = epoch._default_bring_credentials_up_to_date
    spent: list[float] = []

    async def timed() -> None:
        started = time.monotonic()
        await original()
        spent.append(time.monotonic() - started)

    monkeypatch.setattr(epoch, "_default_bring_credentials_up_to_date", timed)

    async def run():
        async with app.app_context(harness.manifest()):
            path.connect(expires_in=-5)
            await harness.reload(door)
            assert not bound(path.entry.title)
            await _until_rotated(path)
            roomy_budgets(monkeypatch)
            await app._probe_and_apply_failed({path.entry.title: path.entry})
            assert bound(path.entry.title)

    asyncio.run(run())
    assert len(spent) == 1
    assert BUDGET * 0.9 <= spent[0] < BUDGET + 0.5
    # One refresh, retried by the engine only when the build blocks the serving loop past the refresh's own
    # 20 s deadline (a loaded host): the build's listing never starts a refresh of its own.
    assert 1 <= path.token_requests <= 2


@pytest.mark.parametrize("door", DOORS)
def test_a_connector_error_for_one_entry_neither_fails_the_reload_nor_stops_the_others(monkeypatch, path, door):
    from tai42_contract.connectors.models import AuthHealthState

    harness = AppHarness(monkeypatch, path)
    other = harness.other_connection(AuthHealthState.RECONNECT_REQUIRED)
    path.connect()
    path.mcp.reset(mcp_fakes.bearer_only("at-old", "at-new"))
    path.token_endpoint()

    async def run():
        async with app.app_context(harness.manifest()):
            path.connect(expires_in=-5)
            await harness.reload(door)
            assert bound(path.entry.title)
            assert app._failed_mcps[other.title]["category"] == "auth"

    asyncio.run(run())
    assert path.token_requests == 1


@pytest.mark.parametrize("door", DOORS)
def test_a_store_error_for_one_entry_is_logged_and_stops_nothing(monkeypatch, path, door, caplog):
    harness = AppHarness(monkeypatch, path)
    other = harness.other_connection(ConnectionError("store unreachable"))
    path.connect()
    path.mcp.reset(mcp_fakes.bearer_only("at-old", "at-new"))
    path.token_endpoint()

    async def run():
        async with app.app_context(harness.manifest()):
            path.connect(expires_in=-5)
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="tai42_skeleton.connectors.runtime.credentials_step"):
                await harness.reload(door)
            assert bound(path.entry.title)
            assert other.title in app._failed_mcps

    asyncio.run(run())
    assert any(
        record.levelno == logging.WARNING and OTHER_CONNECTION_ID in record.getMessage() for record in caplog.records
    )


@pytest.mark.parametrize("door", DOORS)
def test_an_entry_that_fails_the_server_check_is_not_resolved(monkeypatch, path, door, caplog):
    harness = AppHarness(monkeypatch, path)
    harness.entries = []
    forged = harness.forged()
    path.connect(expires_in=-5)
    path.mcp.reset(mcp_fakes.bearer_only("at-old", "at-new"))
    path.token_endpoint()

    async def run():
        async with app.app_context(harness.manifest()):
            await harness.reload(door)
            assert forged.title in app._failed_mcps

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.connectors.token_injection"):
        asyncio.run(run())
    assert path.loads == 0
    assert path.token_requests == 0
    assert path.mcp.authorizations
    assert set(path.mcp.authorizations) == {None}
    assert any(record.levelno == logging.ERROR and forged.title in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("door", DOORS)
def test_a_server_that_lists_without_a_credential_causes_no_refresh_and_no_write(monkeypatch, path, door):
    from tai42_kit.clients.impl.mcp import FastMCPClient

    from tai42_skeleton.connectors.token_injection import call_with_auth

    harness = AppHarness(monkeypatch, path)
    path.connect()
    path.mcp.reset(mcp_fakes.anonymous)
    path.token_endpoint()

    async def run():
        async with app.app_context(harness.manifest()):
            # Used with a fresh token, then reloaded; idle until the token expires; reloaded twice.
            result = await call_with_auth(path.entry, "http", "ping", {}, FastMCPClient())
            assert not result.isError
            await harness.reload(door)
            path.connect(expires_in=-5)
            writes_before = path.store.writes
            await harness.reload(door)
            await harness.reload(door)
            assert bound(path.entry.title)
            assert path.store.writes == writes_before

    asyncio.run(run())
    assert path.token_requests == 0
    assert path.entry.title not in app._mcp_credential_demanded


@pytest.mark.parametrize("door", DOORS)
def test_a_fresh_stored_credential_causes_no_write_at_a_reload(monkeypatch, path, door):
    harness = AppHarness(monkeypatch, path)
    path.connect()
    path.mcp.reset(mcp_fakes.bearer_only("at-old"))

    async def run():
        async with app.app_context(harness.manifest()):
            writes_before = path.store.writes
            await harness.reload(door)
            assert bound(path.entry.title)
            assert path.store.writes == writes_before

    asyncio.run(run())
    assert path.token_requests == 0
