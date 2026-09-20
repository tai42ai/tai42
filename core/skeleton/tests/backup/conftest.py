"""Shared fixtures and helpers for the ``webhooks`` backup-section tests: the
redis-backed and in-memory hooks stores, the execution-gate toggles, and the
trigger-link record builder.
"""

from __future__ import annotations

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.authz import execution as execution_module
from tai42_skeleton.hooks import trigger_links
from tai42_skeleton.hooks.cache import get_hooks_manager as _cache_get_manager  # noqa: F401
from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.managers.redis_hooks_manager import RedisHooksManager
from tai42_skeleton.hooks.settings import HooksSettings

from .._helpers import inline_templated_text
from ..access_control.conftest import FakeAccessControlPg, make_pg_ctx
from ..access_control.conftest import FakeRedis as FakeAccessControlRedis
from ..access_control.conftest import make_client_ctx as make_access_control_client_ctx
from ..hooks.conftest import FakeRedis, make_client_ctx


@pytest.fixture(autouse=True)
def execution_gate_off(monkeypatch):
    """Access control OFF, so the import's token-free-evaluable assertion short-circuits
    and these oracles stay on the section's own envelope. ``policy_store`` turns it on."""
    monkeypatch.setattr(execution_module, "access_control_settings", lambda: AccessControlSettings(enable=False))


@pytest.fixture
def store(monkeypatch):
    """A redis-backed hooks + trigger store shared by the section and the trigger
    module, over one fake redis."""
    import tai42_skeleton.hooks.cache as cache
    import tai42_skeleton.hooks.managers.redis_hooks_manager as rhm

    fake = FakeRedis()
    manager = RedisHooksManager(HooksSettings())
    ctx = make_client_ctx(fake)
    monkeypatch.setattr(trigger_links, "get_hooks_manager", lambda: manager)
    monkeypatch.setattr(cache, "get_hooks_manager", lambda: manager)
    monkeypatch.setattr(trigger_links, "client_ctx", ctx)
    monkeypatch.setattr(rhm, "client_ctx", ctx)
    return type("Store", (), {"manager": manager, "redis": fake, "settings": manager.settings})()


@pytest.fixture
def in_memory_store(monkeypatch):
    import tai42_skeleton.hooks.cache as cache

    manager = InMemoryHooksManager(HooksSettings())
    monkeypatch.setattr(trigger_links, "get_hooks_manager", lambda: manager)
    monkeypatch.setattr(cache, "get_hooks_manager", lambda: manager)
    return type("Store", (), {"manager": manager})()


class _CountingRenderer:
    """Condition renderer that records every render so a test can count them; inline
    ``content`` renders to itself and no stored resource resolves."""

    def __init__(self) -> None:
        self.rendered: list[str] = []

    async def render_templated_text(self, text, locale=None) -> str:
        rendered = inline_templated_text(text)
        self.rendered.append(rendered)
        return rendered


@pytest.fixture
def policy_store(monkeypatch):
    """The execution gate ON over a fake policy store and condition renderer.

    Exposes ``add_policy``, ``rendered`` and ``executed``. Every execution key a record
    names must be seeded — an unseeded key is refused by the gate's existence half."""
    from types import SimpleNamespace

    pg = FakeAccessControlPg()
    renderer = _CountingRenderer()
    # The policy store resolves its Postgres through the registry; the fake transport models a configured deployment.
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    monkeypatch.setattr(policy_module, "client_ctx", make_access_control_client_ctx(FakeAccessControlRedis()))
    monkeypatch.setattr(execution_module, "access_control_settings", lambda: AccessControlSettings(enable=True))
    with tai42_app.bound(SimpleNamespace(storage=SimpleNamespace(resource_manager=renderer))):
        yield SimpleNamespace(add_policy=pg.add_policy, rendered=renderer.rendered, executed=pg.executed)


def _wipe(store) -> None:
    store.redis._strings.clear()
    store.redis._hashes.clear()


def _link_record(name: str, execution_key: str, topic: str = "t") -> dict:
    return {
        "name": name,
        "topic": topic,
        "execution_key": execution_key,
        "execution_key_fingerprint": "fp",
        "require_api_key": False,
        "tool_kwargs": None,
        "created_by": None,
        "created_at": "2026-07-21T00:00:00+00:00",
        "expires_at": None,
    }
