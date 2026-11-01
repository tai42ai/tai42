"""Verifying the plugin environments against the install store once per process, at its first epoch build.

The shared fleet lock, the install store and the verification are faked, so each case
records whether the lock was taken and the rows were read, and the process-level verified
state is reset before every case.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
import pytest

from tai42_skeleton.marketplace.compat import CorePluginBootError
from tai42_skeleton.marketplace.environments import boot_verification
from tai42_skeleton.marketplace.environments.boot_verification import verify_environments_at_boot
from tai42_skeleton.marketplace.environments.findings import Finding
from tai42_skeleton.marketplace.environments.layout import layer_dir
from tai42_skeleton.marketplace.environments.targets import Targets
from tai42_skeleton.marketplace.environments.verify import EnvironmentStatus, Verification

_PLUGINS_DIR = Path("/plugins")
_STALE = Finding(head="plugin environment is out of date", detail="layer holds x", tail=". Run it")
_ORPHANED = Finding(head="server tai42-srv is out of date", detail="no build", tail=". Run it")


class _Recorder:
    """What one case's verification did: the lock taken, the rows read, the findings it returns."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.findings: list[Finding] = []
        self.connect_error: Exception | None = None


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorded = _Recorder()

    @asynccontextmanager
    async def _shared_lock() -> AsyncIterator[None]:
        if recorded.connect_error is not None:
            raise recorded.connect_error
        recorded.events.append("lock")
        yield
        recorded.events.append("unlock")

    class _Store:
        async def list_installed(self) -> list[object]:
            recorded.events.append("rows")
            return ["row"]

    def _verify(plugins_dir: Path | None, rows: list[object]) -> Verification:
        recorded.events.append(f"verify {plugins_dir} {rows}")
        statuses = tuple(
            EnvironmentStatus(label="layer", environment=layer_dir(_PLUGINS_DIR), pins=(), record=None, finding=item)
            for item in recorded.findings
        )
        return Verification(plugins_dir=plugins_dir, targets=Targets(), environments=statuses)

    monkeypatch.setattr(boot_verification._VERIFIED, "done", False)
    monkeypatch.setattr(boot_verification, "component_store_configured", lambda component: component == "skeleton")
    monkeypatch.setattr(boot_verification, "fleet_lock_shared", _shared_lock)
    monkeypatch.setattr(boot_verification, "MarketplaceInstallStore", _Store)
    monkeypatch.setattr(boot_verification, "verify_environments", _verify)
    monkeypatch.setattr(boot_verification, "configured_plugins_dir", lambda: _PLUGINS_DIR)
    return recorded


async def test_a_failing_first_verification_reads_the_rows_under_the_shared_lock_and_refuses(
    recorder: _Recorder,
) -> None:
    recorder.findings = [_STALE, _ORPHANED]

    with pytest.raises(CorePluginBootError) as caught:
        await verify_environments_at_boot()

    assert str(caught.value) == f"{_STALE.boot_text()}\n{_ORPHANED.boot_text()}"
    # The rows are read and the directory verified while a writer is held off.
    assert recorder.events == ["lock", "rows", f"verify {_PLUGINS_DIR} ['row']", "unlock"]


async def test_after_a_passing_verification_a_later_call_takes_no_lock_and_reads_no_rows(
    recorder: _Recorder,
) -> None:
    await verify_environments_at_boot()
    recorder.events.clear()

    await verify_environments_at_boot()

    assert recorder.events == []


async def test_a_refused_verification_is_not_marked_and_the_next_call_verifies_again(recorder: _Recorder) -> None:
    recorder.findings = [_STALE]
    with pytest.raises(CorePluginBootError):
        await verify_environments_at_boot()
    recorder.findings = []
    recorder.events.clear()

    await verify_environments_at_boot()

    assert recorder.events[:2] == ["lock", "rows"]


async def test_with_no_skeleton_database_configured_nothing_is_verified_and_an_info_line_says_so(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(boot_verification, "component_store_configured", lambda component: False)

    with caplog.at_level(logging.INFO, logger=boot_verification.__name__):
        await verify_environments_at_boot()
        await verify_environments_at_boot()

    assert recorder.events == []
    lines = [record.getMessage() for record in caplog.records if record.name == boot_verification.__name__]
    assert lines == ["plugin environments: the 'skeleton' database is not configured — skipping their verification"]


async def test_a_configured_database_that_cannot_be_reached_raises(recorder: _Recorder) -> None:
    recorder.connect_error = psycopg.OperationalError("connection refused")

    with pytest.raises(psycopg.OperationalError):
        await verify_environments_at_boot()

    recorder.connect_error = None
    recorder.events.clear()
    await verify_environments_at_boot()
    assert recorder.events[:2] == ["lock", "rows"]
