"""Extra ``ExtensionRegistry`` coverage: the passing ``validation`` branch (no
missing requested extension), ``get_kind``/``missing_extensions``, and the
``kind_iterator`` taxonomy source.
"""

from __future__ import annotations

import pytest
from tai42_contract.extensions import ExtensionKind

from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.extensions import ExtensionRegistry
from tai42_skeleton.extensions.registry import extension_config, extension_name


def _noop(func, name, desc):
    return func


def test_register_extension_duplicate_name_raises():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    reg.register_extension(_noop, ExtensionKind.WRAPPER, "dup")
    with pytest.raises(TaiValidationError, match="already registered"):
        reg.register_extension(_noop, ExtensionKind.WRAPPER, "dup")


def test_get_extension_unknown_raises():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    with pytest.raises(TaiValidationError, match="not registered"):
        reg.get_extension("ghost")


def test_get_kind_unknown_raises():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    with pytest.raises(TaiValidationError, match="not registered"):
        reg.get_kind("ghost")


def test_requires_body_locality_stored_and_defaults_false():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    reg.register_extension(_noop, ExtensionKind.WRAPPER, "plain")
    reg.register_extension(_noop, ExtensionKind.WRAPPER, "local", requires_body_locality=True)
    assert reg.requires_body_locality("plain") is False
    assert reg.requires_body_locality("local") is True
    # The dict combo-element form keys on its name, like the other lookups.
    assert reg.requires_body_locality({"name": "local", "config": {}}) is True


def test_requires_body_locality_unknown_raises():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    with pytest.raises(TaiValidationError, match="not registered"):
        reg.requires_body_locality("ghost")


def test_extension_decorator_threads_requires_body_locality():
    reg = ExtensionRegistry(requested_extensions=frozenset())

    @reg.extension(kind=ExtensionKind.WRAPPER, requires_body_locality=True)
    def localdeco(func, name, desc):
        return func

    assert reg.requires_body_locality("localdeco") is True


def test_validate_unknown_name_raises():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    with pytest.raises(TaiValidationError, match="ghost"):
        reg.validate(["ghost"])


def test_validation_passes_when_no_missing():
    # The requested extension is registered, so ``missing_extensions`` is empty
    # and ``validation`` returns without raising (the ``if missing`` false path).
    reg = ExtensionRegistry(requested_extensions=frozenset({"monitor"}))
    reg.register_extension(_noop, ExtensionKind.WRAPPER, "monitor")
    assert reg.missing_extensions() == set()
    reg.validation()  # no raise


def test_validation_passes_with_empty_request():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    reg.validation()  # nothing requested -> nothing missing


def test_missing_extensions_reports_unregistered():
    reg = ExtensionRegistry(requested_extensions=frozenset({"monitor", "ghost"}))
    reg.register_extension(_noop, ExtensionKind.WRAPPER, "monitor")
    assert reg.missing_extensions() == {"ghost"}


def test_kind_iterator_yields_every_kind():
    assert set(ExtensionRegistry.kind_iterator()) == set(ExtensionKind)


def test_validate_passes_for_single_backend():
    # A single BACKEND (the single-only kind) is legal — exercises the
    # ``validate`` loop with the cardinality check satisfied.
    reg = ExtensionRegistry(requested_extensions=frozenset())
    reg.register_extension(_noop, ExtensionKind.BACKEND, "async_task")
    reg.validate(["async_task"])


# -- combo elements: bare name vs {"name", "config"} mapping ------------------


def test_element_helpers_extract_name_and_config():
    assert extension_name("monitor") == "monitor"
    assert extension_config("monitor") == {}
    element = {"name": "ask_external", "config": {"verifier": {"verifier": "github"}}}
    assert extension_name(element) == "ask_external"
    assert extension_config(element) == {"verifier": {"verifier": "github"}}


def test_registry_lookups_key_on_dict_element_name():
    # ``validate`` / ``get_extension`` / ``get_kind`` all resolve a combo element's
    # NAME, so a ``{"name", "config"}`` mapping keys the registry identically to
    # the bare name.
    reg = ExtensionRegistry(requested_extensions=frozenset())
    reg.register_extension(_noop, ExtensionKind.TRANSFORMER, "ask_external")
    element = {"name": "ask_external", "config": {"verifier": {"verifier": "github"}}}
    reg.validate([element])  # no raise
    assert reg.get_extension(element) is _noop
    assert reg.get_kind(element) is ExtensionKind.TRANSFORMER


def test_validate_rejects_unknown_dict_element_name():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    with pytest.raises(TaiValidationError, match="ghost"):
        reg.validate([{"name": "ghost", "config": {}}])


# --- declared settings ------------------------------------------------------

_LABELLED_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "minLength": 1}},
    "required": ["label"],
    "additionalProperties": False,
}

_SHAPED_SCHEMA = {
    "type": "object",
    "properties": {"schema": {"$ref": "https://json-schema.org/draft/2020-12/schema"}},
    "required": ["schema"],
    "additionalProperties": False,
}


def _takes_config(func, name, desc, config):
    return func


def _no_config(func, name, desc):
    return func


def _settings_registry() -> ExtensionRegistry:
    reg = ExtensionRegistry(requested_extensions=frozenset())
    reg.register_extension(_takes_config, ExtensionKind.WRAPPER, "labelled", config_schema=_LABELLED_SCHEMA)
    reg.register_extension(_takes_config, ExtensionKind.WRAPPER, "shaped", config_schema=_SHAPED_SCHEMA)
    reg.register_extension(
        _takes_config, ExtensionKind.WRAPPER, "closed", config_schema={"type": "object", "properties": {"a": False}}
    )
    reg.register_extension(_no_config, ExtensionKind.WRAPPER, "plain")
    return reg


def test_registration_refuses_a_config_factory_without_a_schema():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    with pytest.raises(
        TaiValidationError, match=r"^Extension 'x' takes a config parameter but declares no config_schema\.$"
    ):
        reg.register_extension(_takes_config, ExtensionKind.WRAPPER, "x")


def test_registration_refuses_a_schema_on_a_factory_without_config():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    with pytest.raises(
        TaiValidationError,
        match=r"^Extension 'x' declares a config_schema but its factory takes no config parameter\.$",
    ):
        reg.register_extension(_no_config, ExtensionKind.WRAPPER, "x", config_schema={"type": "object"})


def test_registration_refuses_a_schema_breaking_the_json_schema_rules():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    with pytest.raises(TaiValidationError, match=r"^Extension 'x' config_schema: JSON schema"):
        reg.register_extension(_takes_config, ExtensionKind.WRAPPER, "x", config_schema={"type": "nope"})


def test_registration_refuses_a_schema_whose_root_is_not_an_object():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    with pytest.raises(TaiValidationError, match=r"^Extension 'x' config_schema must describe an object\.$"):
        reg.register_extension(_takes_config, ExtensionKind.WRAPPER, "x", config_schema={"type": "string"})


def test_registration_refusal_registers_nothing():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    with pytest.raises(TaiValidationError):
        reg.register_extension(_takes_config, ExtensionKind.WRAPPER, "x")
    assert reg.available_extensions() == []


def test_catalog_serves_the_declared_schema_and_null_without_one():
    schema = {"type": "object", "properties": {"label": {"type": "string"}}}
    reg = ExtensionRegistry(requested_extensions=frozenset())
    reg.register_extension(_takes_config, ExtensionKind.WRAPPER, "labelled", config_schema=schema)
    reg.register_extension(_no_config, ExtensionKind.WRAPPER, "plain")

    assert reg.available_extensions() == [
        {"name": "labelled", "kind": "wrapper", "multiple": True, "config_schema": schema},
        {"name": "plain", "kind": "wrapper", "multiple": True, "config_schema": None},
    ]
    assert reg.config_schema("labelled") == schema
    assert reg.config_schema({"name": "labelled", "config": {}}) == schema
    assert reg.config_schema("plain") is None


def test_stored_schema_is_a_copy_of_the_declaration():
    schema = {"type": "object", "properties": {"label": {"type": "string"}}}
    reg = ExtensionRegistry(requested_extensions=frozenset())
    reg.register_extension(_takes_config, ExtensionKind.WRAPPER, "labelled", config_schema=schema)
    schema["properties"]["label"]["type"] = "integer"
    assert reg.config_schema("labelled") == {"type": "object", "properties": {"label": {"type": "string"}}}


def test_config_schema_unknown_raises():
    reg = ExtensionRegistry(requested_extensions=frozenset())
    with pytest.raises(TaiValidationError, match="not registered"):
        reg.config_schema("ghost")


def test_decorator_threads_config_schema():
    reg = ExtensionRegistry(requested_extensions=frozenset())

    @reg.extension(kind=ExtensionKind.WRAPPER, config_schema=_LABELLED_SCHEMA)
    def labelled(func, name, desc, config):
        return func

    assert reg.config_schema("labelled") == _LABELLED_SCHEMA


def test_validate_config_refuses_settings_on_an_extension_without_a_schema():
    reg = _settings_registry()
    with pytest.raises(TaiValidationError, match=r"^extension 'plain' does not accept config$"):
        reg.validate_config({"name": "plain", "config": {"k": 1}})
    reg.validate_config("plain")
    reg.validate_config({"name": "plain", "config": {}})


_REFUSALS = [
    (
        {"name": "labelled", "config": {}},
        "extension 'labelled' config: value does not match schema at $: breaks the schema's 'required' rule: 'label'",
    ),
    (
        "labelled",
        "extension 'labelled' config: value does not match schema at $: breaks the schema's 'required' rule: 'label'",
    ),
    (
        {"name": "labelled", "config": {"label": "ok", "x": "s3cr3t-unknown"}},
        "extension 'labelled' config: value does not match schema at $: "
        "breaks the schema's 'additionalProperties' rule: false",
    ),
    (
        {"name": "labelled", "config": {"label": 7}},
        "extension 'labelled' config: value does not match schema at $.label: "
        "breaks the schema's 'type' rule: 'string'",
    ),
    (
        {"name": "labelled", "config": {"label": ""}},
        "extension 'labelled' config: value does not match schema at $.label: breaks the schema's 'minLength' rule: 1",
    ),
    (
        {"name": "shaped", "config": {"schema": {"type": "s3cr3t-type"}}},
        "extension 'shaped' config: value does not match schema at $.schema.type: breaks the schema's 'anyOf' rule",
    ),
    (
        {"name": "shaped", "config": {"schema": "s3cr3t-scalar"}},
        "extension 'shaped' config: value does not match schema at $.schema: "
        "breaks the schema's 'type' rule: 'object', 'boolean'",
    ),
    (
        {"name": "closed", "config": {"a": "s3cr3t-closed"}},
        "extension 'closed' config: value does not match schema at $: refused by the schema",
    ),
]


@pytest.mark.parametrize(("element", "message"), _REFUSALS)
def test_validate_config_refuses_without_the_value(element, message):
    reg = _settings_registry()
    with pytest.raises(TaiValidationError) as caught:
        reg.validate_config(element)
    assert str(caught.value) == f"{message} (the value is not shown)"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "s3cr3t" not in str(caught.value)


def test_validate_config_passes_matching_settings():
    reg = _settings_registry()
    reg.validate_config({"name": "labelled", "config": {"label": "ok"}})
    reg.validate_config({"name": "shaped", "config": {"schema": {"type": "object"}}})
    reg.validate_config({"name": "closed", "config": {}})


def test_validate_config_unknown_raises():
    reg = _settings_registry()
    with pytest.raises(TaiValidationError, match="not registered"):
        reg.validate_config("ghost")
