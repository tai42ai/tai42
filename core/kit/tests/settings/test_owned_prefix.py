"""Owned settings prefixes: an env name under an owned prefix must name a registered setting.

A neutral synthetic group (``SYNTH_``) proves the audit: unknown names are refused with
the exact message, aliases and sibling groups' names are accepted, non-owned prefixes are
never audited, and Kubernetes service-link variables are skipped only where the Service's
link family is visibly present in the process environment.
"""

from typing import ClassVar

import pytest
from pydantic import AliasChoices, Field
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import (
    TaiBaseSettings,
    UnknownOwnedSettingError,
    is_kubernetes_service_link,
    kubernetes_service_link_names,
    owned_env_prefixes,
    refuse_unknown_owned_env,
    registered_settings,
    unknown_owned_env_keys,
)
from tai42_kit.settings.registry import _clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    _clear_registry()
    yield
    _clear_registry()


def _define_synth() -> None:
    class SynthSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="SYNTH_")
        env_prefix_owned: ClassVar[bool] = True

        host: str = "localhost"
        level: int = Field(default=1, validation_alias=AliasChoices("SYNTH_LEVEL", "SYNTH_TIER", "ELSEWHERE_LEVEL"))


def _define_open() -> None:
    class OpenSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="OPEN_")

        knob: int = 1


def _group(name: str):
    return next(info for info in registered_settings() if info.name == name)


def test_class_info_carries_prefix_and_ownership():
    _define_synth()
    _define_open()
    synth = _group("SynthSettings")
    assert synth.env_prefix == "SYNTH_"
    assert synth.env_prefix_owned is True
    other = _group("OpenSettings")
    assert other.env_prefix == "OPEN_"
    assert other.env_prefix_owned is False


def test_field_info_lists_every_accepted_env_var():
    _define_synth()
    fields = {f.name: f for f in _group("SynthSettings").fields}
    assert fields["host"].accepted_env_vars == ["SYNTH_HOST"]
    assert fields["level"].accepted_env_vars == ["SYNTH_LEVEL", "SYNTH_TIER", "ELSEWHERE_LEVEL"]


def test_case_sensitive_group_keeps_its_names_verbatim():
    class CaseSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="Case_", case_sensitive=True)

        knob: int = 1

    assert _group("CaseSettings").fields[0].accepted_env_vars == ["Case_knob"]


def test_nested_group_reference_accepts_its_json_env_var():
    class InnerSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="SYNTH_")
        env_prefix_owned: ClassVar[bool] = True

        url: str | None = None

    class OuterSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="SYNTH_")
        env_prefix_owned: ClassVar[bool] = True

        inner: InnerSettings = Field(default_factory=InnerSettings)

    assert _group("OuterSettings").fields[0].accepted_env_vars == ["SYNTH_INNER"]
    assert unknown_owned_env_keys(["SYNTH_INNER", "SYNTH_URL"]) == []


def test_ownership_is_inherited():
    class OwnedBase(TaiBaseSettings):
        registry_exclude: ClassVar[bool] = True
        env_prefix_owned: ClassVar[bool] = True

    class ChildSettings(OwnedBase):
        model_config = SettingsConfigDict(env_prefix="CHILD_")

        knob: int = 1

    assert _group("ChildSettings").env_prefix_owned is True
    assert owned_env_prefixes() == frozenset({"CHILD_"})


def test_owned_group_without_prefix_is_refused_at_definition():
    with pytest.raises(ValueError, match="env_prefix_owned requires a non-empty env_prefix"):

        class NoPrefixSettings(TaiBaseSettings):
            env_prefix_owned: ClassVar[bool] = True

            knob: int = 1


def test_owned_env_prefixes_lists_only_owned_groups():
    _define_synth()
    _define_open()
    assert owned_env_prefixes() == frozenset({"SYNTH_"})


def test_unknown_name_is_refused_with_the_exact_message():
    _define_synth()
    with pytest.raises(UnknownOwnedSettingError) as info:
        refuse_unknown_owned_env(["SYNTH_TYPO", "SYNTH_HOST", "SYNTH_ANOTHER"], source="boot")
    assert str(info.value) == (
        "boot: unknown setting(s) under an owned env prefix: SYNTH_ANOTHER (prefix SYNTH_), "
        "SYNTH_TYPO (prefix SYNTH_). An owned prefix accepts only the env names of its registered "
        "settings groups; check the name against the settings reference."
    )
    assert isinstance(info.value, ValueError)


def test_known_names_pass():
    _define_synth()
    refuse_unknown_owned_env(["SYNTH_HOST", "SYNTH_LEVEL"], source="boot")


def test_every_alias_choice_is_accepted():
    _define_synth()
    assert unknown_owned_env_keys(["SYNTH_TIER", "SYNTH_LEVEL"]) == []


def test_non_owned_prefix_is_never_refused():
    _define_synth()
    _define_open()
    assert unknown_owned_env_keys(["OPEN_TYPO", "UNRELATED", "PATH"]) == []


def test_comparison_ignores_case():
    _define_synth()
    assert unknown_owned_env_keys(["synth_host", "Synth_Level"]) == []
    assert unknown_owned_env_keys(["synth_typo"]) == [("synth_typo", "SYNTH_")]


def test_groups_sharing_an_owned_prefix_accept_each_others_names():
    _define_synth()

    class SynthRedisSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="SYNTH_")
        env_prefix_owned: ClassVar[bool] = True

        redis_url: str | None = None

    assert unknown_owned_env_keys(["SYNTH_HOST", "SYNTH_REDIS_URL"]) == []


def test_an_alias_declared_by_another_group_is_accepted():
    _define_synth()

    class AliasingSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="ALIASING_")

        knob: int = Field(default=1, validation_alias="SYNTH_SHARED_KNOB")

    assert unknown_owned_env_keys(["SYNTH_SHARED_KNOB"]) == []


def test_a_key_is_reported_with_its_longest_owned_prefix():
    _define_synth()

    class SynthSubSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="SYNTH_SUB_")
        env_prefix_owned: ClassVar[bool] = True

        knob: int = 1

    assert unknown_owned_env_keys(["SYNTH_SUB_TYPO", "SYNTH_TYPO"]) == [
        ("SYNTH_SUB_TYPO", "SYNTH_SUB_"),
        ("SYNTH_TYPO", "SYNTH_"),
    ]


def test_no_registered_owned_group_audits_nothing():
    _define_open()
    assert unknown_owned_env_keys(["SYNTH_TYPO", "OPEN_TYPO"]) == []
    refuse_unknown_owned_env(["SYNTH_TYPO"], source="boot")


_POD_ENV = {
    "KUBERNETES_SERVICE_HOST": "10.0.0.1",
    "SYNTH_SERVICE_HOST": "10.0.0.2",
    "SYNTH_SERVICE_PORT": "8000",
    "SYNTH_SERVICE_PORT_HTTP": "8000",
    "SYNTH_PORT": "tcp://10.0.0.2:8000",
    "SYNTH_PORT_8000_TCP": "tcp://10.0.0.2:8000",
    "SYNTH_PORT_8000_TCP_PROTO": "tcp",
    "SYNTH_PORT_8000_TCP_PORT": "8000",
    "SYNTH_PORT_8000_TCP_ADDR": "10.0.0.2",
}


def test_service_link_family_is_skipped_inside_a_pod():
    _define_synth()
    keys = [*_POD_ENV, "SYNTH_SERVICE_HOSTX"]
    assert unknown_owned_env_keys(keys, service_link_env=_POD_ENV) == [("SYNTH_SERVICE_HOSTX", "SYNTH_")]


def test_a_mistyped_owned_setting_is_reported_while_the_family_is_present():
    _define_synth()
    env = {**_POD_ENV, "SYNTH_REDIS_PORT": "6380"}
    assert unknown_owned_env_keys(env, service_link_env=env) == [("SYNTH_REDIS_PORT", "SYNTH_")]


def test_a_lone_port_is_reported():
    _define_synth()
    env = {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "SYNTH_PORT": "tcp://10.0.0.2:8000"}
    assert unknown_owned_env_keys(env, service_link_env=env) == [("SYNTH_PORT", "SYNTH_")]


def test_a_lone_service_host_is_reported():
    _define_synth()
    env = {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "SYNTH_SERVICE_HOST": "10.0.0.2"}
    assert unknown_owned_env_keys(env, service_link_env=env) == [("SYNTH_SERVICE_HOST", "SYNTH_")]


def test_the_family_outside_a_pod_is_reported():
    _define_synth()
    env = {k: v for k, v in _POD_ENV.items() if k != "KUBERNETES_SERVICE_HOST"}
    assert [key for key, _ in unknown_owned_env_keys(env, service_link_env=env)] == sorted(env)


def test_without_service_link_env_every_link_name_is_reported():
    _define_synth()
    reported = [key for key, _ in unknown_owned_env_keys(_POD_ENV)]
    assert reported == sorted(k for k in _POD_ENV if k.startswith("SYNTH_"))


def test_service_link_names_returns_exactly_the_present_families():
    env = {
        **_POD_ENV,
        "ACCESS_POINT_SERVICE_HOST": "10.0.0.3",
        "ACCESS_POINT_PORT": "tcp://10.0.0.3:80",
        "LONELY_SERVICE_HOST": "10.0.0.4",
        "PORTLESS_PORT": "tcp://10.0.0.5:80",
    }
    assert kubernetes_service_link_names(env) == frozenset({"SYNTH", "ACCESS_POINT"})


def test_service_link_names_compare_upper_cased():
    env = {"kubernetes_service_host": "10.0.0.1", "synth_service_host": "10.0.0.2", "Synth_Port": "tcp://x"}
    assert kubernetes_service_link_names(env) == frozenset({"SYNTH"})


def test_service_link_names_are_empty_outside_a_pod():
    env = {k: v for k, v in _POD_ENV.items() if k != "KUBERNETES_SERVICE_HOST"}
    assert kubernetes_service_link_names(env) == frozenset()


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("SYNTH_SERVICE_HOST", True),
        ("SYNTH_SERVICE_PORT", True),
        ("SYNTH_SERVICE_PORT_HTTP", True),
        ("synth_port", True),
        ("SYNTH_PORT_8000_TCP", True),
        ("SYNTH_PORT_53_UDP_PROTO", True),
        ("SYNTH_PORT_9_SCTP_ADDR", True),
        ("SYNTH_SERVICE_HOSTX", False),
        ("SYNTH_PORT_HTTP", False),
        ("SYNTH_PORT_8000_TCP_NAME", False),
        ("SYNTH_REDIS_PORT", False),
        ("OTHER_PORT", False),
    ],
)
def test_is_kubernetes_service_link(key: str, expected: bool):
    assert is_kubernetes_service_link(key, frozenset({"SYNTH"})) is expected


def test_an_accepted_setting_shaped_like_a_link_is_simply_accepted():
    class LinkShapedSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="SYNTH_")
        env_prefix_owned: ClassVar[bool] = True

        port: int = 1

    assert unknown_owned_env_keys(["SYNTH_PORT"]) == []
