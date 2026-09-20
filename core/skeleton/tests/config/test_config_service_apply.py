"""Unit oracles for :class:`~tai42_skeleton.config.service.ConfigService` — the manifest
document doors: ``apply_change`` (read-modify-write) and ``apply_replace`` (whole-document),
and the secret seal that keeps a resolved ``!ENV`` secret from ever baking to disk.

Each test drives the service against a fake config store (the transactional seams), a fake
reload admin, and a fake worker bus, asserting the pipeline validates on the RESOLVED
projection and rejects before any persist, that the mutator is pure / re-runnable, that
``apply_replace`` validates BEFORE it persists, and that a resolved round-trip restores the
operator's marker (or is refused when the secret is stranded).
"""

from __future__ import annotations

import copy
from typing import Any, cast

import pytest
from pyaml_env import parse_config
from pydantic import ValidationError
from tai42_kit.utils.data import dump_manifest

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import OpOutcome
from tai42_skeleton.config.secret_seal import ResolvedSecretError
from tai42_skeleton.operations._broadcast import apply_response

from .._fakes.bus import FakeBus
from .fake_pipeline import (
    FakeConfigStore,
    RetryingConfigStore,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)

# ---------------------------------------------------------------------------
# apply_change
# ---------------------------------------------------------------------------


async def test_apply_change_mutates_validates_persists_reloads_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [*document.get("mcp", []), {"title": "srv", "config": {"url": "http://x"}}]

    result = await service.apply_change(add_server)

    # Persisted the mutated document exactly once, then reloaded locally, then
    # broadcast the reload to the WHOLE fleet with the local result as `local`.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert admin.calls == 1
    assert len(bus.publish_calls) == 1
    op, targets, local = bus.publish_calls[0]
    assert op == {"op": "reload_config"}
    assert targets is None
    assert local is not None
    assert local.outcome == OpOutcome.applied
    # ApplyResult carries the persisted document, the local reload result, and the report.
    assert result.document == {"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}
    assert result.local == {"status": "ok", "env_keys": 0}
    assert result.fleet.op == "reload_config"
    assert result.fleet.ok is True


async def test_apply_result_fanout_is_the_apply_response_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    # fleet_fanout reads the process bus origin to decide local-only vs fleet, so drive
    # the pipeline through that same bus (installed as instance.app.bus) — the value the
    # connector writers thread must equal exactly what apply_response embeds.
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = FakeBus(origin="serve-test", remotes=["serve-w1"])
    monkeypatch.setattr(instance.app, "_bus", bus)
    service, _admin, _bus = _service(store, bus=cast("Any", bus))

    result = await service.apply_replace({"mcp": []})

    assert result.fanout == apply_response(result)["fanout"]
    assert result.fanout["mode"] == "fleet"
    assert {r["name"] for r in result.fanout["results"]} == {"serve-test", "serve-w1"}


async def test_apply_change_invalid_manifest_raises_with_nothing_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    def break_manifest(document: dict[str, Any]) -> None:
        document["tools"] = "not-a-list"  # fails Manifest schema validation

    with pytest.raises(ValidationError):
        await service.apply_change(break_manifest)

    # Validation rejected inside the transaction: nothing persisted, no reload, no broadcast.
    assert store.persisted == []
    assert store.manifest == {"mcp": []}
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_change_mutator_rerun_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    # The store re-runs the mutator (a simulated concurrency conflict) before it
    # persists; a pure mutator yields the same document and persists once.
    store = RetryingConfigStore(manifest={"mcp": []})
    service, admin, _bus = _service(store)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [*document.get("mcp", []), {"title": "srv", "config": {"url": "http://x"}}]

    result = await service.apply_change(add_server)

    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert result.document == {"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}
    assert admin.calls == 1


# ---------------------------------------------------------------------------
# apply_replace
# ---------------------------------------------------------------------------


async def test_apply_replace_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": [{"title": "old", "config": {"url": "http://old"}}]})
    service, admin, bus = _service(store)

    document = {"mcp": [{"title": "new", "config": {"url": "http://new"}}]}
    result = await service.apply_replace(document)

    assert store.manifest == document
    assert admin.calls == 1
    assert bus.publish_calls[0][0] == {"op": "reload_config"}
    assert bus.publish_calls[0][1] is None
    assert result.document == document


async def test_apply_replace_validates_before_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    with pytest.raises(ValidationError):
        await service.apply_replace({"tools": "not-a-list"})

    # A replace has no mutator to abort, so validation must precede the persist.
    assert store.persisted == []
    assert store.manifest == {"mcp": []}
    assert admin.calls == 0
    assert bus.publish_calls == []


# ---------------------------------------------------------------------------
# Secret seal — a resolved !ENV secret never bakes to disk
# ---------------------------------------------------------------------------

# A manifest's mcp section is only ever read through the RESOLVED view, so the natural
# round-trip (read the resolved view → edit → post it back) hands the pipeline resolved
# secret values. The seal retags them back to the operator's !ENV marker before persist,
# and refuses a stranded resolved secret with no marker origin.
_TOKEN = "super-secret-token-value"


def _resolved(document: dict[str, Any]) -> dict[str, Any]:
    """The RESOLVED view a client reads through ``GET /api/manifest`` — ``!ENV``
    markers materialized against the current env, exactly as the live manifest
    exposes them."""
    return cast("dict[str, Any]", parse_config(data=dump_manifest(cast("Any", document))) or {})


def _marker_manifest() -> dict[str, Any]:
    """A manifest whose one mcp server carries an ``!ENV`` auth header marker."""
    return {
        "mcp": [
            {"title": "srv", "config": {"url": "http://x", "headers": {"Authorization": "!ENV ${TOKEN}"}}},
        ]
    }


async def test_apply_change_resolved_round_trip_restores_env_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # The exact set_mcp_config round-trip: the client reads the RESOLVED mcp section,
    # edits it, and posts it back as document["mcp"] = <resolved list>. The seal must
    # restore the operator's !ENV marker so no resolved token bakes to disk.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    store = FakeConfigStore(manifest=_marker_manifest())
    service, _admin, _bus = _service(store)
    resolved = _resolved(store.manifest)
    # The client read the resolved view: its Authorization is the plaintext token.
    assert resolved["mcp"][0]["config"]["headers"]["Authorization"] == _TOKEN

    def post_resolved(document: dict[str, Any]) -> None:
        # set_mcp_config's mutator: wholesale-replace mcp with the client-supplied
        # (resolved) list.
        document["mcp"] = copy.deepcopy(resolved["mcp"])

    result = await service.apply_change(post_resolved)

    # The persisted document restored the !ENV marker; the resolved token never landed.
    assert result.document is not None
    assert result.document["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${TOKEN}"
    assert _TOKEN not in str(store.persisted)


async def test_apply_replace_resolved_round_trip_restores_env_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # A whole-document replace carrying a resolved secret where the current doc has a
    # marker is retagged, so the marker persists.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    store = FakeConfigStore(manifest=_marker_manifest())
    service, _admin, _bus = _service(store)

    replacement = copy.deepcopy(_resolved(store.manifest))
    result = await service.apply_replace(replacement)

    assert result.document is not None
    assert result.document["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${TOKEN}"
    assert _TOKEN not in str(store.persisted)


async def test_apply_replace_stranded_resolved_secret_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # A replacement carrying a resolved secret whose entry has NO marker origin (its
    # identity was renamed away) is a stranded plaintext secret — refused loudly.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    store = FakeConfigStore(manifest=_marker_manifest())
    service, admin, bus = _service(store)

    stranded = copy.deepcopy(_resolved(store.manifest))
    stranded["mcp"][0]["title"] = "renamed"  # no marker origin now; the token is plaintext

    with pytest.raises(ResolvedSecretError) as exc:
        await service.apply_replace(stranded)

    # ValueError-mappable to a 400 by the operations layer, and it names the offending path.
    assert isinstance(exc.value, ValueError)
    assert "mcp[0]" in str(exc.value)
    # Refused before any persist.
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_change_pure_marker_mutator_persists_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pure in-place mutator whose leaves already carry !ENV markers (a connector-style
    # append of a preserved-marker entry) is a retag no-op and passes the leak net with
    # no false rejection — the markers persist verbatim.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    monkeypatch.setenv("OTHER", "other-secret-value")
    store = FakeConfigStore(manifest={"mcp": [{"title": "srv", "config": {"env": {"KEY": "!ENV ${TOKEN}"}}}]})
    service, _admin, _bus = _service(store)

    def append_marker_entry(document: dict[str, Any]) -> None:
        document["mcp"].append({"title": "srv2", "config": {"env": {"KEY2": "!ENV ${OTHER}"}}})

    result = await service.apply_change(append_marker_entry)

    assert result.document is not None
    # Both the untouched and the appended entries keep their !ENV markers verbatim.
    assert result.document["mcp"][0]["config"]["env"]["KEY"] == "!ENV ${TOKEN}"
    assert result.document["mcp"][1]["config"]["env"]["KEY2"] == "!ENV ${OTHER}"
    assert _TOKEN not in str(store.persisted)
