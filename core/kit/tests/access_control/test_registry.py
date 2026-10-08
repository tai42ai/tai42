"""The kit identity-provider registry plugins register into by direct import.

The registry is app-handle-free: registration works with NO bound ``tai42_app`` (no ``bind()``, no
``start()``), so a plugin registers by direct module import in any process.
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from tai42_contract.access_control.identity import AuthIdentity, IdentityProvider

from tai42_kit.access_control.registry import (
    abort_staging,
    begin_staging,
    commit_staging,
    get_identity_provider_factory,
    get_identity_provider_factory_staged,
    iter_identity_provider_names_staged,
    register_identity_provider,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry():  # pyright: ignore[reportUnusedFunction]
    # The registry is module-global state; isolate every test from the others.
    reset_registry()
    yield
    reset_registry()


class _FakeProvider(IdentityProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return AuthIdentity(user_id="u", claims={}) if token == "good" else None


def _fake_factory(*_args: Any, **_kwargs: Any) -> IdentityProvider:
    return _FakeProvider()


# -- Registry ------------------------------------------------------------------


def test_register_then_lookup_returns_the_factory():
    # No bind(), no tai42_app anywhere — a plain module-level call.
    register_identity_provider("fake", _fake_factory)
    assert get_identity_provider_factory("fake") is _fake_factory
    # The factory builds a live provider.
    assert isinstance(get_identity_provider_factory("fake")(), _FakeProvider)


class _OtherProvider(IdentityProvider):
    async def validate_token(self, token: str) -> AuthIdentity | None:
        return None


def _other_factory(*_args: Any, **_kwargs: Any) -> IdentityProvider:
    return _OtherProvider()


def test_reregistering_the_same_factory_is_a_reload_safe_no_op():
    # Reload-safety: the hot-reload primitive pops a plugin's modules and re-executes
    # their bodies, re-running the module-level registration. Before the fix a second
    # register_identity_provider under the same name raised "already registered" and
    # crashed boot; now it is a quiet no-op.
    register_identity_provider("fake", _fake_factory)
    register_identity_provider("fake", _fake_factory)  # no raise
    assert get_identity_provider_factory("fake") is _fake_factory


def test_reregistering_a_reloaded_factory_object_is_a_no_op():
    # The reload primitive mints a FRESH class object each pass, so a reloaded
    # factory is a different object with the same __module__/__qualname__. That still
    # counts as the same provider and must not raise.
    register_identity_provider("dup", _OtherProvider)
    clone = type("_OtherProvider", (IdentityProvider,), dict(_OtherProvider.__dict__))
    clone.__module__ = _OtherProvider.__module__
    clone.__qualname__ = _OtherProvider.__qualname__
    assert clone is not _OtherProvider
    register_identity_provider("dup", clone)  # no raise: same qualified identity
    assert get_identity_provider_factory("dup") is _OtherProvider


def test_different_factory_under_existing_name_still_raises():
    # The real-conflict guard is preserved: a genuinely different provider claiming a
    # taken name is a loud error, not a silent overwrite.
    register_identity_provider("fake", _fake_factory)
    with pytest.raises(ValueError, match="already registered"):
        register_identity_provider("fake", _other_factory)


def test_unknown_name_raises():
    with pytest.raises(KeyError, match="Unknown identity provider"):
        get_identity_provider_factory("nope")


def test_reset_registry_clears():
    register_identity_provider("fake", _fake_factory)
    reset_registry()
    with pytest.raises(KeyError):
        get_identity_provider_factory("fake")
    # After a reset the SAME name re-registers without tripping the dup guard —
    # the reload path the skeleton's start() relies on.
    register_identity_provider("fake", _fake_factory)
    assert get_identity_provider_factory("fake") is _fake_factory


def test_plugin_shape_registers_at_module_import():
    # A plugin module body just calls the directly-imported registration function
    # at import time. Executing such a module body lands the factory in the
    # registry — no app handle, no bind().
    source = (
        "from tai42_kit.access_control.registry import register_identity_provider\n"
        "register_identity_provider('plugin', lambda *a, **k: object())\n"
    )
    module = types.ModuleType("fake_identity_plugin")
    exec(compile(source, "fake_identity_plugin", "exec"), module.__dict__)
    assert get_identity_provider_factory("plugin") is not None


def test_staged_generation_isolates_the_committed_one():
    register_identity_provider("live", _fake_factory)
    begin_staging()
    try:
        assert iter_identity_provider_names_staged() == []
        register_identity_provider("next", _other_factory)
        assert get_identity_provider_factory_staged("next") is _other_factory
        with pytest.raises(KeyError):
            get_identity_provider_factory("next")
        assert get_identity_provider_factory("live") is _fake_factory
        commit_staging()
    finally:
        abort_staging()
    assert iter_identity_provider_names_staged() == ["next"]
    assert get_identity_provider_factory("next") is _other_factory


def test_abort_drops_the_staged_generation():
    register_identity_provider("live", _fake_factory)
    begin_staging()
    register_identity_provider("dropped", _other_factory)
    abort_staging()
    assert iter_identity_provider_names_staged() == ["live"]
