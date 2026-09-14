"""Op-level oracles for the manifest read doors.

``list_failed_mcps`` rides the same fan-out shape as a mutation: this worker's list is
its self-entry payload, and ``targets`` may restrict the query to specific workers. The
read ops carry no destructive/reload-gate metadata.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome
from tai42_skeleton.operations import manifest as manifest_ops
from tai42_skeleton.operations import operation_metadata_of

from ..._fakes.bus import FakeBus
from .conftest import _Admin, _install


async def test_list_failed_mcps_untargeted_reads_local_and_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin(results={"list_failed_mcps": [{"title": "redis", "status": "unavailable"}]})
    bus = _install(monkeypatch, admin=admin)

    result = await manifest_ops.list_failed_mcps()

    # A query rides the same fan-out shape: this worker's list is its self-entry payload.
    assert bus.publish_calls == [
        (
            {"op": "list_failed_mcps"},
            None,
            LocalApplyResult(outcome=OpOutcome.applied, payload=[{"title": "redis", "status": "unavailable"}]),
        )
    ]
    assert result["results"][0]["payload"] == [{"title": "redis", "status": "unavailable"}]


async def test_list_failed_mcps_targeted_to_remote_skips_local(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _Admin(results={"list_failed_mcps": []})
    bus = _install(monkeypatch, admin=admin, bus=FakeBus(remotes=["serve-w1"]))

    result = await manifest_ops.list_failed_mcps(["serve-w1"])

    assert admin.calls == []  # self not targeted → no local read
    assert bus.publish_calls == [({"op": "list_failed_mcps"}, ["serve-w1"], None)]
    assert {r["name"] for r in result["results"]} == {"serve-w1"}


def test_read_ops_are_not_destructive() -> None:
    for op in (
        manifest_ops.get_manifest,
        manifest_ops.get_mcp_config_schema,
        manifest_ops.get_mcp_status,
        manifest_ops.list_failed_mcps,
    ):
        meta = operation_metadata_of(op)
        assert meta.destructive is False, meta.name
        assert meta.reload_gated is False, meta.name
