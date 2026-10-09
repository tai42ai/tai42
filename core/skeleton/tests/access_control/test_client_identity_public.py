"""The client-identity document resolves public by its own declaration, with the Studio shell tier on and off,
and the boot audit refuses a deployment whose replaced acknowledged list leaves it out."""

from __future__ import annotations

import json
import re

import pytest
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.access_control.startup as startup
from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control import verifier as verifier_module
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.access_control.verifier import AccessControlVerifier
from tai42_skeleton.app.route_registry import load_all_routes, route_registry
from tai42_skeleton.connectors.oauth.client_document import DOCUMENT_PATH

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx, make_pg_ctx


@pytest.fixture(autouse=True)
def _reset_reserved_memo():
    verifier_module.reset_registered_reserved_paths()
    yield
    verifier_module.reset_registered_reserved_paths()


def test_the_document_route_is_registered_public_get_only() -> None:
    load_all_routes()
    (meta,) = [m for m in route_registry.routes() if m.path == DOCUMENT_PATH]
    assert meta.authed is False
    assert tuple(meta.methods) == ("GET",)


@pytest.mark.parametrize("spa_shell_public", [True, False])
async def test_the_document_resolves_public_with_the_shell_tier_on_and_off(
    monkeypatch: pytest.MonkeyPatch, spa_shell_public: bool
) -> None:
    load_all_routes()
    settings = AccessControlSettings(spa_shell_public=spa_shell_public)
    assert settings.always_public_route_patterns == ()
    monkeypatch.setattr(verifier_module, "route_registry", route_registry)
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(FakeAccessControlPg()))
    monkeypatch.setattr(policy_module, "client_ctx", make_client_ctx(FakeRedis()))
    verifier = AccessControlVerifier(settings, providers=[])
    assert await verifier.resolve_resource_ids(DOCUMENT_PATH, "GET") == [settings.public_resource_id]


async def test_the_default_acknowledged_list_passes_the_boot_audit() -> None:
    reset_all_settings()
    load_all_routes()
    await startup.check_spa_shell_public()


async def test_a_replaced_acknowledged_list_without_the_document_fails_the_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = [entry for entry in AccessControlSettings().acknowledged_public_routes if entry != DOCUMENT_PATH]
    monkeypatch.setenv("ACCESS_CONTROL_ACKNOWLEDGED_PUBLIC_ROUTES", json.dumps(defaults))
    reset_all_settings()
    load_all_routes()
    with pytest.raises(RuntimeError, match=re.escape(DOCUMENT_PATH)):
        await startup.check_spa_shell_public()
