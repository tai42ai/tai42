"""Every per-generation global stages, commits and aborts in lockstep through ``registry_staging``.

Each global is swapped for a fresh instance of its kit primitive so the test owns its state.
"""

from __future__ import annotations

from typing import Any

import pytest
from tai42_kit.access_control import registry as identity_registry
from tai42_kit.accounts import registry as accounts_registry
from tai42_kit.registry import NamedFactoryRegistry, StagedGeneration, StagedSlot
from tai42_kit.utils import worker_secret_capability as gate_state

from tai42_skeleton.app import registry_staging
from tai42_skeleton.app.route_registry import route_registry
from tai42_skeleton.connectors.providers import registry as connector_registry
from tai42_skeleton.monitoring import registry as monitoring_registry
from tai42_skeleton.operations.registry import operation_registry
from tai42_skeleton.plugins import registry as studio_registry


@pytest.fixture
def globals_(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fresh: dict[str, Any] = {
        "connector": StagedGeneration(dict),
        "identity": NamedFactoryRegistry("Identity provider"),
        "accounts": NamedFactoryRegistry("Accounts provider"),
        "operation": StagedGeneration(dict),
        "shapes": StagedGeneration(list),
        "monitoring": StagedSlot(),
        "studio": StagedSlot(),
        "gate": StagedSlot(),
    }
    monkeypatch.setattr(connector_registry, "_GENERATION", fresh["connector"])
    monkeypatch.setattr(identity_registry, "_PROVIDERS", fresh["identity"])
    monkeypatch.setattr(accounts_registry, "_PROVIDERS", fresh["accounts"])
    monkeypatch.setattr(operation_registry, "_generation", fresh["operation"])
    monkeypatch.setattr(route_registry, "_shapes", fresh["shapes"])
    monkeypatch.setattr(monitoring_registry, "_BACKEND", fresh["monitoring"])
    monkeypatch.setattr(studio_registry, "_REGISTRY", fresh["studio"])
    monkeypatch.setattr(gate_state, "_GATE_STATE", fresh["gate"])
    return fresh


def _write(fresh: dict[str, Any], marker: str) -> None:
    fresh["connector"].write_target()["c"] = marker
    fresh["identity"].register("i", lambda: marker)
    fresh["accounts"].register("a", lambda: marker)
    fresh["operation"].write_target()["o"] = marker
    fresh["shapes"].write_target().append(marker)
    fresh["monitoring"].set(marker)
    fresh["studio"].set(marker)
    fresh["gate"].set(marker == "next")


def _committed(fresh: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector": dict(fresh["connector"].committed()),
        "identity": [name for name, _ in fresh["identity"].items()],
        "accounts": [name for name, _ in fresh["accounts"].items()],
        "operation": dict(fresh["operation"].committed()),
        "shapes": list(fresh["shapes"].committed()),
        "monitoring": fresh["monitoring"].current(),
        "studio": fresh["studio"].current(),
        "gate": fresh["gate"].current(),
    }


def _staging(fresh: dict[str, Any]) -> set[str]:
    open_ = {name for name in ("connector", "operation", "shapes") if fresh[name].staging}
    open_ |= {name for name in ("identity", "accounts") if fresh[name]._generation.staging}
    open_ |= {name for name in ("monitoring", "studio", "gate") if fresh[name]._staging}
    return open_


def test_begin_opens_every_generation_and_abort_drops_them_all(globals_: dict[str, Any]) -> None:
    _write(globals_, "live")
    before = _committed(globals_)
    registry_staging.begin_staging_all()
    assert _staging(globals_) == set(globals_)
    _write(globals_, "next")
    assert _committed(globals_) == before
    registry_staging.abort_staging_all()
    assert _staging(globals_) == set()
    assert _committed(globals_) == before


def test_commit_promotes_every_staged_generation(globals_: dict[str, Any]) -> None:
    _write(globals_, "live")
    registry_staging.begin_staging_all()
    _write(globals_, "next")
    registry_staging.commit_staging_all()
    assert _staging(globals_) == set()
    after = _committed(globals_)
    assert after["connector"] == {"c": "next"}
    assert after["operation"] == {"o": "next"}
    assert after["shapes"] == ["next"]
    assert after["monitoring"] == "next"
    assert after["studio"] == "next"
    assert after["gate"] is True
