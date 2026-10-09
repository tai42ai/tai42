"""The recorded auth providers: written to the building core, read building-first, read-only."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.access_control.identity import IdentityProvider
from tai42_contract.accounts import LoginAttachingProvider

from tai42_skeleton.app.lifecycle import serving_core_access
from tai42_skeleton.app.lifecycle.serving_core_access import ServingCoreAccessMixin


def _never_called(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("a stand-in member that is never called")


class _ProbeBase(ServingCoreAccessMixin):
    """The access mixin alone, over a stand-in building core and live epoch."""

    def __init__(self, building: Any) -> None:
        self._building = building


# The lifecycle's other abstract members are never reached by the accessor under test.
_Probe = type("_Probe", (_ProbeBase,), dict.fromkeys(_ProbeBase.__abstractmethods__, _never_called))


def _core() -> Any:
    return SimpleNamespace(active_auth_providers={})


def _provider() -> IdentityProvider:
    return SimpleNamespace()  # type: ignore[return-value]


@pytest.fixture
def live_core(monkeypatch: pytest.MonkeyPatch) -> Any:
    core = _core()
    epoch = SimpleNamespace(core=core)
    monkeypatch.setattr(serving_core_access, "current_epoch", lambda: epoch)
    return core


def test_record_during_a_build_writes_the_building_core(live_core: Any) -> None:
    building = _core()
    probe = _Probe(building)
    provider = _provider()
    probe.record_auth_provider("p1", provider)
    assert building.active_auth_providers == {"p1": provider}
    assert live_core.active_auth_providers == {}


def test_read_is_building_first(live_core: Any) -> None:
    live_provider, built_provider = _provider(), _provider()
    live_core.active_auth_providers["p1"] = live_provider
    building = _core()
    building.active_auth_providers["p1"] = built_provider
    assert _Probe(building).recorded_auth_providers()["p1"] is built_provider


def test_read_outside_a_build_is_the_live_core(live_core: Any) -> None:
    provider = _provider()
    live_core.active_auth_providers["p1"] = provider
    recorded = _Probe(None).recorded_auth_providers()
    assert dict(recorded) == {"p1": provider}


def test_recorded_providers_are_read_only(live_core: Any) -> None:
    recorded = _Probe(None).recorded_auth_providers()
    with pytest.raises(TypeError):
        recorded["p2"] = _provider()  # type: ignore[index]


def test_recorded_providers_follow_later_records(live_core: Any) -> None:
    probe = _Probe(None)
    recorded = probe.recorded_auth_providers()
    provider = _provider()
    probe.record_auth_provider("late", provider)
    assert recorded["late"] is provider


# -- the four operations readers read through the accessor ---------------------


# A login-attaching accounts provider (so also an accounts provider) whose methods are never called.
_Accounts = type(
    "_Accounts",
    (LoginAttachingProvider,),
    dict.fromkeys(LoginAttachingProvider.__abstractmethods__, _never_called),
)


@pytest.fixture
def accessor_providers(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from tai42_skeleton.app.instance import app

    plain = _provider()
    accounts = _Accounts()
    recorded: dict[str, Any] = {"b-accounts": accounts, "a-plain": plain}

    def _recorded() -> Mapping[str, IdentityProvider]:
        return recorded

    monkeypatch.setattr(app, "recorded_auth_providers", _recorded)
    return recorded


def test_principals_reader_uses_the_accessor(accessor_providers: dict[str, Any]) -> None:
    from tai42_skeleton.operations import principals

    assert principals._login_attaching_providers() == [("b-accounts", accessor_providers["b-accounts"])]


def test_member_actions_reader_uses_the_accessor(accessor_providers: dict[str, Any]) -> None:
    from tai42_skeleton.operations import member_actions

    assert member_actions._active_accounts_provider_items() == [("b-accounts", accessor_providers["b-accounts"])]


def test_login_reader_uses_the_accessor(accessor_providers: dict[str, Any]) -> None:
    from tai42_skeleton.operations import login

    assert login._active_accounts_providers() == [accessor_providers["b-accounts"]]


def test_setup_reader_uses_the_accessor(accessor_providers: dict[str, Any]) -> None:
    from tai42_skeleton.operations import setup

    assert setup._active_login_attaching_provider() is accessor_providers["b-accounts"]
