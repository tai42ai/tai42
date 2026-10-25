"""Op-level oracles for the manifest read doors.

``list_failed_mcps`` rides the same fan-out shape as a mutation: this worker's list is
its self-entry payload, and ``targets`` may restrict the query to specific workers. The
read ops carry no destructive/reload-gate metadata.
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from tai42_contract.app import tai42_app

from tai42_skeleton.app.bus import LocalApplyResult, OpOutcome
from tai42_skeleton.operations import manifest as manifest_ops
from tai42_skeleton.operations import operation_metadata_of
from tai42_skeleton.operations.response_models_group_a import PreservedManifestView

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


class _PreservedStore:
    """A config manager whose preserved read returns the given document, or raises ``FileNotFoundError``."""

    def __init__(self, document: dict | None) -> None:
        self._document = document

    def read_manifest_preserved(self) -> dict:
        if self._document is None:
            raise FileNotFoundError("manifest.yml")
        return deepcopy(self._document)


def _install_preserved(monkeypatch: pytest.MonkeyPatch, document: dict | None) -> None:
    impl = SimpleNamespace(config=SimpleNamespace(config_manager=_PreservedStore(document)))
    monkeypatch.setattr(tai42_app, "_impl", impl)


def test_preserved_view_serves_extension_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXT_TOKEN", "s3cr3t-token-value")
    _install_preserved(
        monkeypatch,
        {
            "extensions_modules": ["pkg.ext"],
            "backend_module": "pkg.backend",
            "tools": [
                {"title": "a", "module": "m", "extensions": {"echo": ["first"]}},
                {"title": "b", "module": "n"},
            ],
            "mcp": [
                {
                    "title": "srv",
                    "config": {"url": "https://x"},
                    "extensions": {
                        "fetch": [["first"], [{"name": "second", "config": {"token": "!ENV ${EXT_TOKEN}"}}]]
                    },
                }
            ],
            "api_tools": {"extensions": {"list_events": ["first"]}},
        },
    )

    view = manifest_ops._preserved_manifest_view()

    assert view["extension_settings"] == {
        "extensions_modules": ["pkg.ext"],
        "backend_module": "pkg.backend",
        "tools": [{"title": "a", "extensions": {"echo": [["first"]]}}],
        "mcp": [
            {
                "title": "srv",
                "extensions": {"fetch": [["first"], [{"name": "second", "config": {"token": "!ENV ${EXT_TOKEN}"}}]]},
            }
        ],
        "api_tools": {"list_events": [["first"]]},
    }
    assert "s3cr3t-token-value" not in json.dumps(view)
    PreservedManifestView.model_validate(view)


def test_preserved_view_empty_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_preserved(monkeypatch, None)

    view = manifest_ops._preserved_manifest_view()

    assert view["extension_settings"] == {
        "extensions_modules": [],
        "backend_module": None,
        "tools": [],
        "mcp": [],
        "api_tools": {},
    }


def test_preserved_view_malformed_map_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_preserved(monkeypatch, {"tools": [{"title": "a", "module": "m", "extensions": {"echo": []}}]})

    with pytest.raises(ValidationError, match=r"extensions\['echo'\] must not be empty"):
        manifest_ops._preserved_manifest_view()


def test_preserved_view_entry_without_title_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_preserved(monkeypatch, {"tools": [{"module": "m", "extensions": {"echo": ["first"]}}]})

    with pytest.raises(KeyError, match="title"):
        manifest_ops._preserved_manifest_view()
