"""The access-control route index follows every record the route registry takes.

A served boot builds the index during its startup audits, then mounts the MCP transports and
the sub-MCP router, recording them in the registry. The index must reflect those records the
moment they exist: a mounted surface missing from it resolves to nothing, so an
unauthenticated request to an MCP endpoint is refused 403 instead of 401, and a GET under the
sub-MCP mount falls through to the public Studio-shell tier.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tai42_skeleton.access_control import role_gate
from tai42_skeleton.app.route_registry import MOUNT_METHODS, route_registry


@pytest.fixture(autouse=True)
def _registry_copy(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(route_registry, "_routes", dict(route_registry._routes))
    monkeypatch.setattr(route_registry, "_control_plane_mount", route_registry._control_plane_mount)
    role_gate.reset_route_index()
    yield
    role_gate.reset_route_index()


def test_a_mount_recorded_after_the_index_was_built_resolves_as_a_served_surface() -> None:
    # The startup audits build the index first; the path is then only the public shell's.
    before = role_gate.resolve_served_surface("/probe-xport/svc/sse", "GET")
    assert before is None or before.public
    route_registry.record_mounted(
        path="/probe-xport/{path:path}", methods=MOUNT_METHODS, name="probe_mount", summary="probe mount"
    )
    served = role_gate.resolve_served_surface("/probe-xport/svc/sse", "GET")
    assert served is not None
    assert served.mounted
    assert served.name == "probe_mount"


def test_a_transport_recorded_after_the_index_was_built_resolves_as_a_served_surface() -> None:
    assert role_gate.resolve_served_surface("/probe-endpoint", "POST") is None
    route_registry.record_mounted(
        path="/probe-endpoint", methods=["GET", "POST", "DELETE"], name="probe_transport", summary="probe"
    )
    served = role_gate.resolve_served_surface("/probe-endpoint", "POST")
    assert served is not None
    assert served.name == "probe_transport"


def test_a_record_taken_while_the_index_enumerates_is_not_missed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Another thread (an epoch build) records a mount after the index has listed the
    # registry but before the build finishes: the next lookup must still resolve it.
    listed = role_gate.load_all_routes
    raced: list[bool] = []

    def listing_then_a_racing_record():
        routes = listed()
        if not raced:
            raced.append(True)
            route_registry.record_mounted(
                path="/probe-raced/{path:path}", methods=MOUNT_METHODS, name="probe_raced", summary="probe raced"
            )
        return routes

    monkeypatch.setattr(role_gate, "load_all_routes", listing_then_a_racing_record)
    role_gate.resolve_served_surface("/probe-raced/x", "GET")
    assert raced
    served = role_gate.resolve_served_surface("/probe-raced/x", "GET")
    assert served is not None
    assert served.mounted
    assert served.name == "probe_raced"
