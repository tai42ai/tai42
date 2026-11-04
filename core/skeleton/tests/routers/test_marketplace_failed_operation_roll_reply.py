"""What a caller reads when a marketplace operation failed after its config changes ran their fleet recycle roll.

The failed operation's error reply carries the roll it ran (``recycle``) and the workers still
awaiting a respawn after it, on the HTTP door and on the projected tool every MCP, agent and
background door runs. When the roll converged with the writer needing its own restart, the
writer's exit follows that error reply as it follows a success reply; a roll that stopped arms
nothing. The HTTP door is served under :class:`WriterExitMiddleware`, as the base app serves it.

The real installer and :class:`ConfigService` pipeline run over the in-process model of
PostgreSQL's session advisory locks (the roll-hold world of the marketplace installer tests).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient
from tai42_contract.manifest import ApiToolsConfig

import tai42_skeleton.operations.marketplace as mkt_ops
import tai42_skeleton.routers.marketplace as router
from tai42_skeleton.app import instance
from tai42_skeleton.app.error_envelope import install_error_handlers
from tai42_skeleton.app.writer_exit import WRITER_EXIT_REASON
from tai42_skeleton.marketplace.errors import LocalStateError
from tai42_skeleton.middleware.writer_exit import WriterExitMiddleware
from tai42_skeleton.operations import OperationRegistry, operation_metadata_of
from tai42_skeleton.operations.projection import project_operations

from ..config.fake_pipeline import _reset_settings_after  # noqa: F401  — autouse fixture, active on import
from ..marketplace._specs import make_resolved
from ..marketplace.test_installer_roll_hold import (  # noqa: F401  — fixtures
    _RECYCLE_KEY,
    _relay_spec,
    _World,
    keys,
    world,
)

_FAILURE = "attribution row is corrupt"


@pytest.fixture
def failing(world: _World, monkeypatch: pytest.MonkeyPatch) -> _World:  # noqa: F811
    """The world serving the marketplace routes, an install failing at its record after its env change.

    The writer runs on a value the unwind does not put back, so the failed install's reload
    reports the key on the writer and its roll defers the writer's restart.
    """
    monkeypatch.setattr(mkt_ops, "Installer", lambda *args, **kwargs: world.installer)
    monkeypatch.setattr(mkt_ops, "component_store_configured", lambda component: True)
    monkeypatch.setattr(instance.app, "_bus", world.bus)
    world.registry.resolved = make_resolved(_relay_spec())
    world.store.record_error = LocalStateError(_FAILURE)
    world.bindings.bound["serve-writer"] = "EARLIER"
    return world


def _install(client: TestClient) -> Any:
    return client.post("/api/marketplace/install", json={"ref": _relay_spec().ref, "env": {_RECYCLE_KEY: "NEW"}})


def _client() -> TestClient:
    async def install(request: Request) -> Response:
        return await router.marketplace_install(request)

    app = Starlette(
        routes=[Route("/api/marketplace/install", install, methods=["POST"])],
        middleware=[Middleware(WriterExitMiddleware)],
    )
    install_error_handlers(app)
    return TestClient(app, raise_server_exceptions=False)


def _projected_install() -> Any:
    registry = OperationRegistry()
    registry.register(operation_metadata_of(mkt_ops.marketplace_install))
    registered: dict[str, Any] = {}

    class _Tools:
        def tool(self, *, force: bool, name: str, tags: Any, annotations: Any) -> Any:
            def decorator(func: Any) -> Any:
                registered[name] = func
                return func

            return decorator

    project_operations(
        SimpleNamespace(tools=_Tools()),
        ApiToolsConfig(include=["marketplace_install"], expose_destructive=True),
        registry=registry,
    )
    return registered["marketplace_install"]


def test_a_failed_http_install_whose_roll_converged_carries_it_and_runs_the_writer_exit_after_the_reply(
    failing: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    exits = failing.bus.exit_requests

    with _client() as client:
        response = _install(client)

    assert response.status_code == 500
    body = response.json()
    assert _FAILURE in body["error"]
    assert [(e["name"], e["status"], e["keys"]) for e in body["recycle"]["recycle"]] == [
        ("serve-writer", "self-deferred", [_RECYCLE_KEY])
    ]
    assert body["recycle"]["skipped"] == ["serve-b"]
    assert body["awaiting_respawn"] == []
    assert failing.probe.writer_self_deferred == [True]
    # The error reply was answered, then the writer's exit ran.
    assert exits == [WRITER_EXIT_REASON]
    failing.assert_the_key_is_free()


async def test_a_failed_projected_install_whose_roll_converged_runs_the_writer_exit_before_the_error_raises(
    failing: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    exits = failing.bus.exit_requests

    with pytest.raises(ToolError, match=_FAILURE):
        await _projected_install()(ref=_relay_spec().ref, env={_RECYCLE_KEY: "NEW"})

    assert exits == [WRITER_EXIT_REASON]


def test_a_failed_http_install_whose_roll_stopped_names_the_workers_awaiting_and_arms_nothing(
    failing: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    exits = failing.bus.exit_requests
    failing.bindings.bound["serve-b"] = "EARLIER"
    failing.roll_stops = "slow"

    with _client() as client:
        response = _install(client)

    assert response.status_code == 500
    body = response.json()
    assert body["recycle"]["recycle_stopped"] == {"kind": "serve", "name": "serve-b", "detail": "slow"}
    assert sorted(worker["name"] for worker in body["awaiting_respawn"]) == ["serve-b", "serve-writer"]
    assert exits == []
