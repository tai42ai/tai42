"""Settings registry: every concrete ``TaiBaseSettings`` subclass self-registers
as a plain-data group. Extraction honors aliases, marks secrets, skips
non-JSON-safe defaults, and references nested settings as their own group."""

import enum

import pytest
from pydantic import AliasChoices, AliasPath, Field, SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import SettingsConfigDict

from tai42_kit.clients.settings import RedisConnectionSettings
from tai42_kit.settings import TaiBaseSettings, registered_settings
from tai42_kit.settings.registry import (
    _clear_registry,
    _factory_needs_validated_data,
    _register,
    _resolve_default,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear the registry around every test so locally-defined classes don't leak
    into (or out of) other tests."""
    _clear_registry()
    yield
    _clear_registry()


def _group(name: str):
    for info in registered_settings():
        if info.name == name:
            return info
    raise AssertionError(f"group {name!r} not registered")


def _field(group_name: str, field_name: str):
    for f in _group(group_name).fields:
        if f.name == field_name:
            return f
    raise AssertionError(f"field {field_name!r} not on group {group_name!r}")


def test_subclass_appears_with_extracted_fields():
    class WidgetSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="WIDGET_")

        host: str = "localhost"
        port: int = 8080

    host = _field("WidgetSettings", "host")
    assert host.env_var == "WIDGET_HOST"
    assert host.type == "string"
    assert host.required is False
    assert host.secret is False
    assert host.default == "localhost"

    port = _field("WidgetSettings", "port")
    assert port.env_var == "WIDGET_PORT"
    assert port.type == "integer"
    assert port.default == 8080


def test_required_field_has_no_default():
    class NeedsSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="NEEDS_")

        token: str

    token = _field("NeedsSettings", "token")
    assert token.required is True
    assert token.default is None


def test_optional_field_reports_underlying_type():
    class MaybeSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="MAYBE_")

        count: int | None = None

    count = _field("MaybeSettings", "count")
    assert count.type == "integer"  # null-union resolves to the underlying type
    assert count.default is None


def test_validation_alias_overrides_prefixed_env_var():
    class AliasSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="TAI_MCP_")

        manifest_path: str = Field(default="/m", validation_alias="TAI_MANIFEST_PATH")

    field = _field("AliasSettings", "manifest_path")
    assert field.env_var == "TAI_MANIFEST_PATH"  # alias wins over prefix formula


def test_secretstr_field_is_secret_and_hides_default():
    class SecretSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="SECRET_")

        api_key: SecretStr = SecretStr("baked-in")

    field = _field("SecretSettings", "api_key")
    assert field.secret is True
    assert field.default is None  # a secret default is never emitted


def test_json_schema_extra_marks_secret():
    class FlaggedSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="FLAGGED_")

        token: str = Field(default="t", json_schema_extra={"secret": True})

    field = _field("FlaggedSettings", "token")
    assert field.secret is True
    assert field.default is None


def test_registry_exclude_base_does_not_appear():
    from typing import ClassVar

    class AbstractBase(TaiBaseSettings):
        registry_exclude: ClassVar[bool] = True

        shared: str = "x"

    assert all(info.name != "AbstractBase" for info in registered_settings())


def test_concrete_subclass_of_excluded_base_registers():
    # Own-attribute exclude, not inherited: a concrete subclass of an excluded
    # connection-settings base still registers as its own group.
    class AppRedisSettings(RedisConnectionSettings):
        model_config = SettingsConfigDict(env_prefix="APP_REDIS_")

    group = _group("AppRedisSettings")
    redis_url = _field("AppRedisSettings", "redis_url")
    assert redis_url.env_var == "APP_REDIS_REDIS_URL"
    assert any(f.name == "decode_responses" for f in group.fields)


def test_nested_settings_field_references_own_group_once():
    class ChildSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="CHILD_")

        flag: bool = True

    class ParentSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="PARENT_")

        child: ChildSettings = ChildSettings()
        label: str = "p"

    # The sub-model appears as its own group exactly once — no duplicate.
    child_groups = [info for info in registered_settings() if info.name == "ChildSettings"]
    assert len(child_groups) == 1

    # The parent's field is a non-editable reference to that group.
    child_field = _field("ParentSettings", "child")
    assert child_field.nested_group == "ChildSettings"
    assert child_field.env_var == ""
    assert child_field.type == "object"
    assert child_field.required is False
    assert child_field.default is None
    assert child_field.secret is False


class Color(enum.StrEnum):
    RED = "red"
    BLUE = "blue"


class Priority(enum.IntEnum):
    LOW = 1
    HIGH = 2


def test_optional_enum_reports_underlying_scalar_type():
    class OptEnumSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="OPTENUM_")

        opt_enum: Color | None = None
        opt_int_enum: Priority | None = None

    opt = _field("OptEnumSettings", "opt_enum")
    assert opt.type == "string"  # str-Enum $ref resolved to its underlying scalar
    opt_int = _field("OptEnumSettings", "opt_int_enum")
    assert opt_int.type == "integer"  # IntEnum $ref resolved to its underlying scalar


def test_plain_enum_reports_underlying_scalar_type():
    class PlainEnumSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="PLAINENUM_")

        plain_enum: Color = Color.RED

    field = _field("PlainEnumSettings", "plain_enum")
    assert field.type == "string"


def test_plain_int_enum_reports_integer_type():
    class PlainIntEnumSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="PLAININTENUM_")

        plain_int_enum: Priority = Priority.LOW

    field = _field("PlainIntEnumSettings", "plain_int_enum")
    assert field.type == "integer"  # non-optional IntEnum resolves to its underlying scalar


def test_enum_default_uses_value():
    class EnumDefaultSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="ENUMDEF_")

        plain_enum: Color = Color.RED

    field = _field("EnumDefaultSettings", "plain_enum")
    assert field.default == "red"  # the JSON-safe ``.value``, not None


def test_alias_choices_reports_first_choice_as_env_var():
    class ChoiceSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="PROBE_")

        probe: str = Field(default="x", validation_alias=AliasChoices("A_ONE", "A_TWO"))

    field = _field("ChoiceSettings", "probe")
    # pydantic-settings ignores env_prefix once a validation_alias is set; the
    # primary env var is the first choice, not the prefix formula.
    assert field.env_var == "A_ONE"


def test_alias_choices_with_no_string_choice_raises():
    # An AliasChoices whose every choice is a path (list) alias has no plain
    # string env var to report; extraction must raise rather than guess one.
    with pytest.raises(ValueError, match="no string choice"):

        class PathAliasSettings(TaiBaseSettings):
            model_config = SettingsConfigDict(env_prefix="PATHALIAS_")

            probe: str = Field(default="x", validation_alias=AliasChoices(AliasPath("nested", "key")))


def test_case_sensitive_env_var_is_not_upper_cased():
    class CaseSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="probe_", case_sensitive=True)

        Endpoint: str = "x"

    field = _field("CaseSettings", "Endpoint")
    assert field.env_var == "probe_Endpoint"  # declared casing preserved


def test_validated_data_default_factory_registers_without_crashing():
    def _factory(data):  # pydantic 2.10+ validated-data factory (1-arg)
        return data["missing"]

    class FactorySettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="FACTORY_")

        value: str = Field(default_factory=_factory)

    # Registration must not crash even though the factory raises when invoked
    # with no validated data; the display default is simply absent.
    field = _field("FactorySettings", "value")
    assert field.default is None


def test_zero_arg_default_factory_error_propagates():
    def _broken_factory():  # plain 0-arg factory with a genuine bug
        raise RuntimeError("factory is broken")

    # A 0-arg factory is evaluated at registration; a real error inside it must
    # surface loudly, never be hidden behind a ``None`` display default.
    with pytest.raises(RuntimeError, match="factory is broken"):

        class BrokenFactorySettings(TaiBaseSettings):
            model_config = SettingsConfigDict(env_prefix="BROKENFACTORY_")

            value: str = Field(default_factory=_broken_factory)


def test_list_builtin_default_factory_reports_empty_list():
    class ListFactorySettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="LISTFACTORY_")

        tags: list[str] = Field(default_factory=list)

    # ``list`` is a builtin whose signature cannot be introspected; it is the
    # plain no-arg factory pydantic calls, so its default is an empty list — not
    # silently absent as if it needed validated data.
    field = _field("ListFactorySettings", "tags")
    assert field.default == []


def test_dict_builtin_default_factory_reports_empty_dict():
    class DictFactorySettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="DICTFACTORY_")

        meta: dict[str, str] = Field(default_factory=dict)

    field = _field("DictFactorySettings", "meta")
    assert field.default == {}


def test_set_builtin_default_factory_is_called_and_yields_empty_set():
    # ``set`` is a builtin (uninspectable signature): it does NOT need validated
    # data, so it is called and returns an empty set rather than being skipped.
    # (The field-level default is stripped by JSON-safety, so assert the resolve.)
    assert _factory_needs_validated_data(set) is False
    assert _resolve_default(FieldInfo(default_factory=set)) == set()


def test_single_defaulted_positional_factory_is_called():
    def _factory(data=None):  # one positional param WITH a default: plain 0-required-arg
        return "resolved"

    class DefaultedPosSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="DEFAULTEDPOS_")

        value: str = Field(default_factory=_factory)

    # A lone positional parameter that carries a default is genuinely callable
    # with no args (pydantic calls it), so its real value is reported, not None.
    field = _field("DefaultedPosSettings", "value")
    assert field.default == "resolved"


def test_reregistration_replaces_not_duplicates():
    class ReloadSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="RELOAD_")

        value: int = 1

    before = len(registered_settings())
    # Simulate a module re-import re-running the hook with the same qualname.
    _register(ReloadSettings)
    _register(ReloadSettings)
    after = len(registered_settings())
    assert after == before  # keyed by qualname — replaced, never accumulated


def test_non_json_safe_default_is_emitted_as_none():
    class BinaryDefaultSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="BINARY_")

        blob: bytes = b"seed"

    # A non-secret default with no JSON representation (bytes) cannot go on the
    # wire for the settings-schema, so extraction emits None rather than the raw
    # value.
    field = _field("BinaryDefaultSettings", "blob")
    assert field.default is None


def test_fieldless_subclass_is_not_registered():
    class MarkerSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="MARKER_")

    # No fields means nothing env-configurable, so the empty-model_fields guard
    # skips the class — it never appears as a group.
    assert all(info.name != "MarkerSettings" for info in registered_settings())
