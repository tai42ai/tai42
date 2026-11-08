"""What a caller reads when a marketplace operation's config changes landed and their roll did not run.

The writer's database session drops once the operation's changes are committed, so the fleet hold
cannot be downgraded and the fleet recycle roll the changes owe never starts. The caller — over
HTTP, the CLI that reads the HTTP reply, or the projected tool every MCP, agent and background
door runs — reads that the changes landed and the roll did not complete, never a bare masked 500,
while the driver's text stays in the server log. The HTTP door is served under
:class:`WriterExitMiddleware`, as the base app serves it.

The real installer and :class:`ConfigService` pipeline run over the in-process model of
PostgreSQL's session advisory locks (the roll-hold world of the marketplace installer tests).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from tai42_contract.manifest import ApiToolsConfig

import tai42_skeleton.operations.marketplace as mkt_ops
import tai42_skeleton.routers.marketplace as router
from tai42_skeleton.app import instance
from tai42_skeleton.app.error_envelope import install_error_handlers
from tai42_skeleton.marketplace import locks
from tai42_skeleton.marketplace.errors import UnrunRollError
from tai42_skeleton.middleware.writer_exit import WriterExitMiddleware
from tai42_skeleton.operations import OperationRegistry, operation_metadata_of
from tai42_skeleton.operations.projection import project_operations

from .._fakes.bus import FakeBus
from .._writer_exit_edge import admit_change
from ..config.fake_pipeline import _reset_settings_after  # noqa: F401  — autouse fixture, active on import
from ..marketplace._specs import make_resolved
from ..marketplace.test_installer_roll_hold import (  # noqa: F401  — fixtures
    _RECYCLE_KEY,
    _relay_spec,
    _World,
    keys,
    world,
)

_DRIVER_TEXT = "consuming input failed: server closed the connection unexpectedly (host db-internal-7)"
_UNRUN = "config changes landed, but the fleet recycle roll they owe did not complete: OperationalError"


class _Session:
    """Whether the writer's database session drops once an operation's changes are committed."""

    def __init__(self) -> None:
        self.drops = True


@pytest.fixture
def session(world: _World, monkeypatch: pytest.MonkeyPatch) -> _Session:  # noqa: F811
    """The writer's session, dropping by default: the fleet hold's downgrade then raises the driver's error."""
    state = _Session()
    downgrade = locks._AdvisoryHold.downgrade

    async def dropping_downgrade(self: locks._AdvisoryHold) -> None:
        if state.drops:
            raise psycopg.OperationalError(_DRIVER_TEXT)
        await downgrade(self)

    monkeypatch.setattr(locks._AdvisoryHold, "downgrade", dropping_downgrade)
    return state


@pytest.fixture
def dropped_session(world: _World, session: _Session, monkeypatch: pytest.MonkeyPatch) -> _World:  # noqa: F811
    """The world serving the marketplace routes on its bus, its writer's session dropping after each commit."""
    monkeypatch.setattr(instance.app, "_bus", world.bus)
    monkeypatch.setattr(mkt_ops, "Installer", lambda *args, **kwargs: world.installer)
    monkeypatch.setattr(mkt_ops, "component_store_configured", lambda component: True)
    world.registry.resolved = make_resolved(_relay_spec())
    return world


async def _post(path: str, body: dict[str, Any]) -> httpx.Response:
    """POST ``body`` to the marketplace route at ``path`` on an app with the error envelope and writer-exit edge."""
    app = Starlette(
        routes=[
            Route("/api/marketplace/install", router.marketplace_install, methods=["POST"]),
            Route("/api/marketplace/update", router.marketplace_update, methods=["POST"]),
            Route("/api/marketplace/uninstall", router.marketplace_uninstall, methods=["POST"]),
            Route("/api/marketplace/upgrade-all", router.marketplace_upgrade_all, methods=["POST"]),
        ],
        middleware=[Middleware(WriterExitMiddleware)],
    )
    install_error_handlers(app)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body)


async def test_an_http_install_whose_session_dropped_after_commit_says_the_change_landed_and_the_roll_did_not_run(
    dropped_session: _World, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        response = await _post("/api/marketplace/install", {"ref": _relay_spec().ref, "env": {_RECYCLE_KEY: "NEW"}})

    assert response.status_code == 500
    assert response.json() == {"error": _UNRUN}
    # The change landed; the driver's text reaches the log, never the reply.
    assert dropped_session.config_store.env[_RECYCLE_KEY] == "NEW"
    assert dropped_session.probe.rolls == 0
    assert _DRIVER_TEXT in caplog.text
    dropped_session.assert_the_key_is_free()


async def test_an_http_update_whose_session_dropped_after_commit_says_the_change_landed_and_the_roll_did_not_run(
    dropped_session: _World, session: _Session, writer_exit_edge: object
) -> None:
    # The setup install runs below any door, under the edge fixture admitting it; the update under test is
    # served over HTTP.
    session.drops = False
    admit_change(dropped_session.bus)
    await dropped_session.install("NEW")
    session.drops = True
    newer = _relay_spec("1.1.0")
    dropped_session.registry.resolved = make_resolved(newer)

    response = await _post("/api/marketplace/update", {"ref": newer.ref, "env": {_RECYCLE_KEY: "NEWER"}})

    assert response.status_code == 500
    assert response.json() == {"error": _UNRUN}
    assert dropped_session.config_store.env[_RECYCLE_KEY] == "NEWER"


async def test_a_failed_http_install_whose_session_dropped_before_its_roll_says_both(
    dropped_session: _World,
) -> None:
    dropped_session.store.record_error = RuntimeError("attribution write failed")
    # The sibling still runs on a value the undo does not put back, so the failed install owes a roll.
    dropped_session.bindings.bound["serve-b"] = "EARLIER"

    response = await _post("/api/marketplace/install", {"ref": _relay_spec().ref, "env": {_RECYCLE_KEY: "NEW"}})

    assert response.status_code == 500
    assert response.json() == {"error": f"{_UNRUN}\nthe operation failed before its roll ran: RuntimeError"}
    dropped_session.assert_the_key_is_free()


async def test_a_projected_install_whose_session_dropped_after_commit_says_the_change_landed_and_the_roll_did_not_run(
    dropped_session: _World,
) -> None:
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

    with pytest.raises(ToolError) as raised:
        await registered["marketplace_install"](ref=_relay_spec().ref, env={_RECYCLE_KEY: "NEW"})

    assert str(raised.value) == _UNRUN
    assert dropped_session.config_store.env[_RECYCLE_KEY] == "NEW"


class _UnrunRollInstaller:
    """An installer whose uninstall and upgrade-all changes landed and whose roll the session drop stopped."""

    async def _unrun(self) -> Any:
        try:
            raise psycopg.OperationalError(_DRIVER_TEXT)
        except psycopg.OperationalError as dropped:
            raise UnrunRollError(_UNRUN) from dropped

    async def uninstall(self, ref: str) -> Any:
        return await self._unrun()

    async def upgrade_all(self) -> Any:
        return await self._unrun()


@pytest.mark.parametrize("path", ["/api/marketplace/uninstall", "/api/marketplace/upgrade-all"])
async def test_an_http_uninstall_or_upgrade_all_whose_roll_did_not_run_says_the_change_landed(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(instance.app, "_bus", FakeBus())
    monkeypatch.setattr(mkt_ops, "Installer", lambda *args, **kwargs: _UnrunRollInstaller())
    monkeypatch.setattr(mkt_ops, "component_store_configured", lambda component: True)

    response = await _post(path, {"ref": _relay_spec().ref} if path.endswith("uninstall") else {})

    assert response.status_code == 500
    assert response.json() == {"error": _UNRUN}
