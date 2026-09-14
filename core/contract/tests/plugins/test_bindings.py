"""Tests for the item-kind vocabulary and the kind->manifest binding table
(``PluginItemKind`` / ``KIND_MANIFEST_BINDINGS`` / ``ManifestBinding`` / ``DATA_BLOCK_BY_KIND``)."""

from __future__ import annotations

import pytest


def test_kind_enum_has_the_sixteen_kinds():
    from tai42_contract.plugins import PluginItemKind

    assert {k.value for k in PluginItemKind} == {
        "tool",
        "agent",
        "extension",
        "connector",
        "channel",
        "backend",
        "storage",
        "monitoring",
        "webhook-verifier",
        "config",
        "identity",
        "studio-plugin",
        "router",
        "middleware",
        "mcp-server",
        "sandbox",
    }


def test_mcp_server_binds_to_the_mcp_entry_mode():
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, ManifestBinding, PluginItemKind

    assert KIND_MANIFEST_BINDINGS[PluginItemKind.MCP_SERVER] == ManifestBinding(
        field="mcp", mode="mcp_entry", payload="data"
    )


def test_connector_binds_to_the_descriptor_entry_mode():
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, ManifestBinding, PluginItemKind

    assert KIND_MANIFEST_BINDINGS[PluginItemKind.CONNECTOR] == ManifestBinding(
        field="connectors", mode="descriptor_entry", payload="data"
    )


def test_every_kind_has_a_payload():
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS

    # Every binding names its payload axis, so a future kind added without one
    # fails here rather than defaulting silently.
    for kind, binding in KIND_MANIFEST_BINDINGS.items():
        assert binding.payload in ("module", "data"), kind


def test_data_payload_iff_data_mode():
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS

    # The payload axis and the data modes are one fact: a binding carries
    # ``data`` exactly when its mode is one of the declarative-block modes.
    for kind, binding in KIND_MANIFEST_BINDINGS.items():
        assert (binding.payload == "data") == (binding.mode in ("mcp_entry", "descriptor_entry")), kind


def test_data_kinds_are_the_data_payload_bindings():
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, PluginItemKind, data_kinds

    assert data_kinds() == {PluginItemKind.MCP_SERVER, PluginItemKind.CONNECTOR}
    assert data_kinds() == {kind for kind, binding in KIND_MANIFEST_BINDINGS.items() if binding.payload == "data"}


def test_data_block_by_kind_keys_are_exactly_the_data_kinds():
    from tai42_contract.plugins import DATA_BLOCK_BY_KIND, PluginItemKind, data_kinds

    assert set(DATA_BLOCK_BY_KIND) == data_kinds()
    assert DATA_BLOCK_BY_KIND[PluginItemKind.MCP_SERVER] == "mcp"
    assert DATA_BLOCK_BY_KIND[PluginItemKind.CONNECTOR] == "provider"


def test_bindings_cover_every_kind_exactly():
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, PluginItemKind

    # Completeness pin: every kind — including ``router`` and ``middleware`` —
    # has a binding, so a future member added without one fails here rather than
    # at a skeleton install.
    assert set(KIND_MANIFEST_BINDINGS) == set(PluginItemKind)


def test_router_and_middleware_bind_to_module_lists():
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS, ManifestBinding, PluginItemKind

    assert KIND_MANIFEST_BINDINGS[PluginItemKind.ROUTER] == ManifestBinding(
        field="routers_modules", mode="module_list", payload="module"
    )
    assert KIND_MANIFEST_BINDINGS[PluginItemKind.MIDDLEWARE] == ManifestBinding(
        field="middlewares_modules", mode="module_list", payload="module"
    )


def test_binding_fields_are_real_manifest_fields():
    from tai42_contract.manifest import Manifest
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS

    # Every manifest-wired binding names an actual Manifest field, so the
    # installer can never patch a field the manifest model would reject. The
    # connector descriptor binding targets ``connectors``, a real Manifest field.
    manifest_fields = set(Manifest.model_fields)
    assert "connectors" in manifest_fields
    for kind, binding in KIND_MANIFEST_BINDINGS.items():
        if binding.field is not None:
            assert binding.field in manifest_fields, f"{kind}: {binding.field}"


def test_binding_mode_matches_manifest_field_cardinality():
    from typing import get_origin

    from tai42_contract.manifest import Manifest
    from tai42_contract.plugins import KIND_MANIFEST_BINDINGS

    # A scalar mode must target a non-list Manifest field (a single-module slot
    # like ``str | None``); every list/row mode must target a list-typed field.
    # This guards the binding table against a mode/field cardinality mismatch
    # that would make an installer append to a scalar or overwrite a list. The
    # descriptor binding's ``connectors`` field is now a real list-typed Manifest
    # field, so the list-cardinality assertion below covers it like every other
    # list mode.
    scalar_modes = {"scalar_module"}
    list_modes = {"config_row", "module_list", "package_list", "mcp_entry", "descriptor_entry"}
    for kind, binding in KIND_MANIFEST_BINDINGS.items():
        if binding.field is None:
            continue
        annotation = Manifest.model_fields[binding.field].annotation
        is_list_field = get_origin(annotation) is list
        if binding.mode in scalar_modes:
            assert not is_list_field, f"{kind}: scalar mode targets list field {binding.field}"
        elif binding.mode in list_modes:
            assert is_list_field, f"{kind}: list mode targets non-list field {binding.field}"
        else:  # pragma: no cover - a new mode must be classified here
            raise AssertionError(f"{kind}: unclassified mode {binding.mode!r}")


def test_binding_field_none_iff_env_selected():
    from tai42_contract.plugins import ManifestBinding

    with pytest.raises(ValueError, match="env_selected"):
        ManifestBinding(field=None, mode="module_list", payload="module")
    with pytest.raises(ValueError, match="env_selected"):
        ManifestBinding(field="tools", mode="env_selected", payload="module")
    assert ManifestBinding(field=None, mode="env_selected", payload="module").field is None


def test_binding_payload_must_match_mode():
    from tai42_contract.plugins import ManifestBinding

    with pytest.raises(ValueError, match="payload must be 'data'"):
        ManifestBinding(field="mcp", mode="mcp_entry", payload="module")
    with pytest.raises(ValueError, match="payload must be 'data'"):
        ManifestBinding(field="connectors", mode="descriptor_entry", payload="module")
    with pytest.raises(ValueError, match="payload must be 'data'"):
        ManifestBinding(field="tools", mode="config_row", payload="data")
