"""The typed fleet ops: each publisher's op dumps to its wire dict, every op declares its effects.

The table holds one row per publishing site. A row proves the site's model dumps to the exact
dict that travels the bus and validates back into the same model, so a receiving worker of any
version applies it; the effects column is what a post-apply handler is told the op changed.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.backend import FleetOpApplied, FleetOpEffect

from tai42_skeleton.app.bus.ops import (
    FLEET_OP_ADAPTER,
    ClearTemplateCache,
    DeregisterMcp,
    EvictTemplate,
    FleetOp,
    ListFailedMcps,
    Recycle,
    ReloadConfig,
    ReloadFailedMcps,
    ReloadMcp,
    ReloadTool,
    RemoveTool,
    fleet_op_wire,
)
from tai42_skeleton.app.instance import app

_REGISTRY = frozenset({FleetOpEffect.registry})
_TEMPLATES = frozenset({FleetOpEffect.templates})
_NONE: frozenset[FleetOpEffect] = frozenset()

# (publishing site, the op it builds, the wire dict, the effects it declares)
_PUBLISHERS: list[tuple[str, FleetOp, dict[str, Any], frozenset[FleetOpEffect]]] = [
    ("operations/config.py", ReloadConfig(op="reload_config"), {"op": "reload_config"}, _REGISTRY),
    ("operations/backend.py", ReloadConfig(op="reload_config"), {"op": "reload_config"}, _REGISTRY),
    ("config/service/broadcast.py profile", ReloadConfig(op="reload_config"), {"op": "reload_config"}, _REGISTRY),
    ("config/service/broadcast.py persist", ReloadConfig(op="reload_config"), {"op": "reload_config"}, _REGISTRY),
    ("app/lifecycle/bus_subscription.py resync", ReloadConfig(op="reload_config"), {"op": "reload_config"}, _REGISTRY),
    (
        "operations/backup.py",
        ClearTemplateCache(op="clear_template_cache"),
        {"op": "clear_template_cache"},
        _TEMPLATES,
    ),
    (
        "operations/tools.py reload",
        ReloadTool(op="reload_tool", kind="example_tool", name="probe"),
        {"op": "reload_tool", "kind": "example_tool", "name": "probe"},
        _REGISTRY,
    ),
    (
        "operations/tools.py remove",
        RemoveTool(op="remove_tool", kind="example_tool", name="probe"),
        {"op": "remove_tool", "kind": "example_tool", "name": "probe"},
        _REGISTRY,
    ),
    (
        "operations/templates.py write",
        EvictTemplate(op="evict_template", path="a.j2"),
        {"op": "evict_template", "path": "a.j2"},
        _TEMPLATES,
    ),
    (
        "operations/templates.py delete",
        EvictTemplate(op="evict_template", path="a.j2"),
        {"op": "evict_template", "path": "a.j2"},
        _TEMPLATES,
    ),
    (
        "operations/templates.py delete dir",
        EvictTemplate(op="evict_template", path="dir", prefix=True),
        {"op": "evict_template", "path": "dir", "prefix": True},
        _TEMPLATES,
    ),
    (
        "operations/templates.py clear",
        ClearTemplateCache(op="clear_template_cache"),
        {"op": "clear_template_cache"},
        _TEMPLATES,
    ),
    (
        "operations/storage.py write",
        EvictTemplate(op="evict_template", path="r.j2"),
        {"op": "evict_template", "path": "r.j2"},
        _TEMPLATES,
    ),
    (
        "operations/storage.py delete dir",
        EvictTemplate(op="evict_template", path="d", prefix=True),
        {"op": "evict_template", "path": "d", "prefix": True},
        _TEMPLATES,
    ),
    (
        "operations/manifest/runtime.py reload",
        ReloadMcp(op="reload_mcp", title="svc"),
        {"op": "reload_mcp", "title": "svc"},
        _REGISTRY,
    ),
    (
        "operations/manifest/runtime.py retry",
        ReloadFailedMcps(op="reload_failed_mcps"),
        {"op": "reload_failed_mcps"},
        _REGISTRY,
    ),
    (
        "operations/manifest/runtime.py deregister",
        DeregisterMcp(op="deregister_mcp", title="svc"),
        {"op": "deregister_mcp", "title": "svc"},
        _REGISTRY,
    ),
    ("operations/manifest/reads.py", ListFailedMcps(op="list_failed_mcps"), {"op": "list_failed_mcps"}, _NONE),
    (
        "operations/presets/fanout.py reload",
        ReloadTool(op="reload_tool", kind="preset", name="acme"),
        {"op": "reload_tool", "kind": "preset", "name": "acme"},
        _REGISTRY,
    ),
    (
        "operations/presets/fanout.py remove",
        RemoveTool(op="remove_tool", kind="preset", name="acme"),
        {"op": "remove_tool", "kind": "preset", "name": "acme"},
        _REGISTRY,
    ),
    ("app/recycle.py", Recycle(op="recycle"), {"op": "recycle"}, _NONE),
]


@pytest.mark.parametrize(("site", "op", "wire", "effects"), _PUBLISHERS, ids=[row[0] for row in _PUBLISHERS])
def test_each_publisher_op_dumps_to_its_wire_dict_and_declares_its_effects(
    site: str, op: FleetOp, wire: dict[str, Any], effects: frozenset[FleetOpEffect]
) -> None:
    assert fleet_op_wire(op) == wire, site
    assert FLEET_OP_ADAPTER.validate_python(wire) == op, site
    assert type(op).effects == effects, site


def test_every_op_model_has_a_publisher_row() -> None:
    assert {type(op) for _site, op, _wire, _effects in _PUBLISHERS} == {
        ReloadConfig,
        ReloadMcp,
        DeregisterMcp,
        ReloadTool,
        RemoveTool,
        ReloadFailedMcps,
        ListFailedMcps,
        EvictTemplate,
        ClearTemplateCache,
        Recycle,
    }


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ({"op": "rebuild_everything"}, "rebuild_everything"),
        ({}, "op"),
        ({"op": "reload_mcp"}, "title"),
        ({"op": "evict_template", "path": 7}, "path"),
        ({"op": "reload_tool", "kind": "preset", "name": "acme", "extra": 1}, "extra"),
    ],
    ids=["unknown-op", "no-op", "missing-field", "wrong-type", "unknown-field"],
)
def test_an_unknown_or_malformed_op_is_refused(raw: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        FLEET_OP_ADAPTER.validate_python(raw)


async def test_a_handler_registered_through_the_lifecycle_facet_receives_the_declared_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A neutral post-apply consumer: it keeps what each applied op declared it changed.
    seen: list[tuple[str, frozenset[FleetOpEffect]]] = []

    async def _no_dispatch(op: FleetOp) -> None:
        return None

    monkeypatch.setattr(app, "_dispatch_bus_op", _no_dispatch)
    with tai42_app.bound(app):

        def _probe_consumer(applied: FleetOpApplied) -> None:
            seen.append((applied.op, applied.effects))

        tai42_app.lifecycle.on_fleet_op_applied(_probe_consumer)
        key = f"{_probe_consumer.__module__}.{_probe_consumer.__qualname__}"
        try:
            await app._apply_bus_op({"op": "evict_template", "path": "a.j2"}, deadline=1e9)
            await app._apply_bus_op({"op": "reload_tool", "kind": "preset", "name": "acme"}, deadline=1e9)
            await app._apply_bus_op({"op": "list_failed_mcps"}, deadline=1e9)
        finally:
            app._fleet_op_applied_handlers.pop(key, None)

    assert seen == [
        ("evict_template", _TEMPLATES),
        ("reload_tool", _REGISTRY),
        ("list_failed_mcps", _NONE),
    ]
