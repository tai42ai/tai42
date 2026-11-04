"""Unit oracles for the two stages of a manifest write through
:class:`~tai42_skeleton.config.service.ConfigService`: ``persist_change`` (the write stage) and
``converge`` (the live convergence of a persisted change), which ``apply_change`` composes.

Each test drives the service against a fake config store, a fake reload admin and a recording
worker bus, asserting the write stage persists and converges nothing, and the convergence reloads
and broadcasts the persisted change once.
"""

from __future__ import annotations

from typing import Any

import pytest

from tai42_skeleton.config.service.results import PersistedChange

from .fake_pipeline import (
    FakeConfigStore,
    _oauth_descriptor,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)


def _add_server(document: dict[str, Any]) -> None:
    document["mcp"] = [*document.get("mcp", []), {"title": "srv", "config": {"url": "http://x"}}]


async def test_persist_change_persists_the_mutation_and_converges_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    persisted = await service.persist_change(_add_server)

    assert persisted == PersistedChange(document={"mcp": [{"title": "srv", "config": {"url": "http://x"}}]})
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_converge_reloads_and_broadcasts_the_persisted_change_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)
    persisted = PersistedChange(document={"mcp": []}, span_release_error="OSError: the span stayed held")

    result = await service.converge(persisted)

    assert store.persisted == []
    assert admin.calls == 1
    assert len(bus.publish_calls) == 1
    assert bus.publish_calls[0][0] == {"op": "reload_config"}
    assert result.document == {"mcp": []}
    assert result.span_release_error == "OSError: the span stayed held"


async def test_persist_change_of_a_leaving_oauth_connector_writes_its_secret_mark_and_persists_its_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"connectors": [_oauth_descriptor("acme")]}, env={})
    service, admin, bus = _service(store)

    leaving = service._apply_change_with_leaving
    written: list[Any] = []

    async def recording_leaving(*args: Any) -> Any:
        written.append(await leaving(*args))
        return written[-1]

    monkeypatch.setattr(service, "_apply_change_with_leaving", recording_leaving)

    def drop(document: dict[str, Any]) -> None:
        document["connectors"] = []

    persisted = await service.persist_change(drop)

    assert persisted.document["connectors"] == []
    assert "ACME_CLIENT_SECRET" in store.env["TAI_ENV_SECRET_KEYS"].split(",")
    [document] = written
    assert persisted.document == document
    assert admin.calls == 0
    assert bus.publish_calls == []
