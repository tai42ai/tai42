"""Tests for ``PluginSpec`` — its fields, migrations, premium flag, delivery axis,
contract-vs-kind and connector-origin rules, and the ``Manifest`` connectors round-trip."""

from __future__ import annotations

from typing import Any

import pytest


def _spec_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "spec_version": 1,
        "namespace": "tai42",
        "name": "toolbox",
        "display_name": "TAI Toolbox",
        "package": "tai42-toolbox",
        "version": "0.1.0",
        "description": "Generic tools and tool extensions.",
        "icon": "assets/toolbox.svg",
        "license": "Apache-2.0",
        "repository": "https://github.com/tai42ai/tai42/tree/main/plugins/toolbox",
        "contract": ">=0.1,<0.2",
        "categories": ["utilities"],
        "tags": ["uuid", "http"],
        "permissions": {"network": True},
        "provides": [
            {
                "kind": "tool",
                "name": "generate_uuid",
                "module": "tai42_toolbox.tools.generate_uuid",
                "description": "Generate a random UUID.",
                "tags": ["uuid"],
            }
        ],
    }
    base.update(overrides)
    return base


def _mcp_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": "mcp-server",
        "name": "postgres_mcp",
        "mcp": {"url": "https://mcp.example.com"},
        "description": "A hosted MCP server.",
    }
    item.update(overrides)
    return item


def _all_mcp_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs = _spec_kwargs(provides=[_mcp_item()], **overrides)
    kwargs.pop("contract", None)
    return kwargs


def _connector_provider(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "acme",
        "display_name": "Acme",
        "icon_url": "https://cdn.example.com/acme.png",
        "kind": "oauth",
        "origin": "system",
        "category": "productivity",
        "oauth": {"authorize": "https://acme.example.com/authorize", "token": "https://acme.example.com/token"},
        "client_id_env": "ACME_CLIENT_ID",
        "client_secret_env": "ACME_CLIENT_SECRET",
        "sub_services": {
            "mail": {
                "id": "mail",
                "display_name": "Mail",
                "scopes": ["mail.read"],
                "mcp_server": {"type": "http", "url": "https://acme.example.com/mcp"},
            }
        },
    }
    base.update(overrides)
    return base


def _connector_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": "connector",
        "name": "acme",
        "provider": _connector_provider(),
        "description": "Acme connector.",
    }
    item.update(overrides)
    return item


def _connector_spec_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs = _spec_kwargs(namespace="tai42", name="acme", provides=[_connector_item()], categories=["productivity"])
    # Descriptor-only by default: an all-data spec ships no package.
    kwargs["package"] = None
    kwargs.update(overrides)
    return kwargs


# (field name, a valid value for that field) for every regex-validated token
# field on PluginSpec. A trailing or embedded newline must be rejected: the
# validators use ``fullmatch``, so ``$``-before-final-newline can no longer
# smuggle a newline past an anchored pattern.
_SPEC_TOKEN_FIELDS: list[tuple[str, str]] = [
    ("namespace", "tai42"),
    ("name", "toolbox"),
    ("package", "tai42-toolbox"),
    ("version", "0.1.0"),
    ("license", "Apache-2.0"),
    ("icon", "assets/toolbox.svg"),
    ("migrations", "migrations"),
]


def test_spec_constructs_and_is_frozen():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    spec = PluginSpec(**_spec_kwargs())
    assert spec.package == "tai42-toolbox"
    with pytest.raises(ValidationError):
        spec.name = "changed"


def test_ref_is_namespace_slash_name():
    from tai42_contract.plugins import PluginSpec

    assert PluginSpec(**_spec_kwargs()).ref == "tai42/toolbox"


def test_spec_version_must_be_one():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="spec_version"):
        PluginSpec(**_spec_kwargs(spec_version=2))


def test_unknown_top_level_key_rejected():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="pricing"):
        PluginSpec(**_spec_kwargs(pricing="free"))


def test_namespace_and_name_must_be_lowercase_slugs():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    for bad in ("Tai42", "2go", "with space", ""):
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(namespace=bad))
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(name=bad))


def test_display_name_optional_bounded_and_single_line():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    kwargs = _spec_kwargs()
    del kwargs["display_name"]
    assert PluginSpec(**kwargs).display_name is None
    assert PluginSpec(**_spec_kwargs(display_name=None)).display_name is None
    assert PluginSpec(**_spec_kwargs(display_name="TAI Toolbox")).display_name == "TAI Toolbox"
    with pytest.raises(ValidationError, match="at most 80"):
        PluginSpec(**_spec_kwargs(display_name="x" * 81))
    with pytest.raises(ValidationError, match="display_name must be non-empty"):
        PluginSpec(**_spec_kwargs(display_name="   "))
    with pytest.raises(ValidationError, match="display_name must be a single line"):
        PluginSpec(**_spec_kwargs(display_name="two\nlines"))


def test_icon_accepts_https_or_relative_path_and_rejects_others():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    kwargs = _spec_kwargs()
    del kwargs["icon"]
    assert PluginSpec(**kwargs).icon is None
    assert PluginSpec(**_spec_kwargs(icon=None)).icon is None
    assert PluginSpec(**_spec_kwargs(icon="https://tai42.ai/toolbox.png")).icon == "https://tai42.ai/toolbox.png"
    assert PluginSpec(**_spec_kwargs(icon="assets/icon.svg")).icon == "assets/icon.svg"
    for bad in ("/abs/icon.png", "http://tai42.ai/x.png", "assets\\icon.png"):
        with pytest.raises(ValidationError, match="relative POSIX path"):
            PluginSpec(**_spec_kwargs(icon=bad))
    for bad in ("../escape.png", "assets/../secret.png"):
        with pytest.raises(ValidationError, match=r"'\.\.' segment"):
            PluginSpec(**_spec_kwargs(icon=bad))


def test_package_must_be_a_normalized_dist_name():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    for bad in ("tai42_toolbox", "Tai-Toolbox", "tai-toolbox-", "-tai"):
        with pytest.raises(ValidationError, match="normalized"):
            PluginSpec(**_spec_kwargs(package=bad))


def test_categories_require_one_to_three_unique_slugs():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match=r"1\.\.3"):
        PluginSpec(**_spec_kwargs(categories=[]))
    with pytest.raises(ValidationError, match=r"1\.\.3"):
        PluginSpec(**_spec_kwargs(categories=["a", "b", "c", "d"]))
    with pytest.raises(ValidationError, match="unique"):
        PluginSpec(**_spec_kwargs(categories=["utilities", "utilities"]))
    with pytest.raises(ValidationError, match="category"):
        PluginSpec(**_spec_kwargs(categories=["Utilities"]))


def test_tags_are_bounded_unique_and_lowercase():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="at most 10"):
        PluginSpec(**_spec_kwargs(tags=[f"t{i}" for i in range(11)]))
    with pytest.raises(ValidationError, match="unique"):
        PluginSpec(**_spec_kwargs(tags=["dup", "dup"]))
    with pytest.raises(ValidationError, match="tag"):
        PluginSpec(**_spec_kwargs(tags=["UPPER"]))


def test_permissions_default_all_false_and_reject_unknown_keys():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginPermissions, PluginSpec

    kwargs = _spec_kwargs()
    del kwargs["permissions"]
    spec = PluginSpec(**kwargs)
    assert spec.permissions == PluginPermissions()
    assert (spec.permissions.network, spec.permissions.subprocess, spec.permissions.filesystem) == (
        False,
        False,
        False,
    )
    with pytest.raises(ValidationError, match="gpu"):
        PluginSpec(**_spec_kwargs(permissions={"gpu": True}))


def test_provides_must_be_non_empty():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="at least one"):
        PluginSpec(**_spec_kwargs(provides=[]))


def test_provides_rejects_duplicate_kind_name():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    kwargs = _spec_kwargs()
    kwargs["provides"] = [kwargs["provides"][0], dict(kwargs["provides"][0])]
    with pytest.raises(ValidationError, match="duplicate"):
        PluginSpec(**kwargs)


@pytest.mark.parametrize(
    ("kind", "module"), [("router", "tai42_plugin.routers.door"), ("middleware", "tai42_plugin.mw.trace")]
)
def test_provides_accepts_router_and_middleware_items(kind: str, module: str):
    from tai42_contract.plugins import PluginItemKind, PluginSpec

    item_data: dict[str, Any] = {
        "kind": kind,
        "name": f"{kind}_item",
        "module": module,
        "description": f"A plugin {kind}.",
    }
    if kind == "router":
        # A router item now declares its mount; a middleware item never does.
        item_data["routes"] = {"base": "relay", "paths": [{"path": "/events", "methods": ["POST"], "public": True}]}
    spec = PluginSpec(**_spec_kwargs(provides=[item_data]))
    item = spec.provides[0]
    assert item.kind is PluginItemKind(kind)
    assert item.module == module


def test_provides_rejects_duplicate_router_items():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    router_item = {
        "kind": "router",
        "name": "door",
        "module": "tai42_plugin.routers.door",
        "description": "A plugin router.",
        "routes": {"base": "relay", "paths": [{"path": "/events", "methods": ["POST"], "public": True}]},
    }
    with pytest.raises(ValidationError, match="duplicate"):
        PluginSpec(**_spec_kwargs(provides=[router_item, dict(router_item)]))


def test_migrations_is_optional_and_defaults_to_none():
    from tai42_contract.plugins import PluginSpec

    # A plugin that owns no tables never declares a chain: the field is absent
    # (base kwargs omit it) and also accepts an explicit None.
    assert PluginSpec(**_spec_kwargs()).migrations is None
    assert PluginSpec(**_spec_kwargs(migrations=None)).migrations is None


@pytest.mark.parametrize("value", ["migrations", "db/migrations", "sql/schema.d"])
def test_migrations_accepts_package_relative_dir(value: str):
    from tai42_contract.plugins import PluginSpec

    assert PluginSpec(**_spec_kwargs(migrations=value)).migrations == value


def test_migrations_rejects_non_relative_or_unsafe_paths():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    # Format-only shape guard: absolute, trailing-slash, backslash/drive, and
    # empty values are rejected loudly (never silently normalized).
    for bad in ("/abs/migrations", "migrations/", "db\\migrations", "c:/migrations", ""):
        with pytest.raises(ValidationError, match="package-relative POSIX directory path"):
            PluginSpec(**_spec_kwargs(migrations=bad))
    for bad in ("../migrations", "db/../secret"):
        with pytest.raises(ValidationError, match=r"'\.\.' segment"):
            PluginSpec(**_spec_kwargs(migrations=bad))


def test_migrations_validation_touches_no_filesystem():
    from tai42_contract.plugins import PluginSpec

    # The path need not exist: the contract validates shape only, so a
    # well-formed but non-existent directory round-trips (existence is the
    # runner's discovery-time concern). round-trip via model_validate mirrors
    # the DB-row hydration path, which can touch no filesystem.
    value = "this/dir/does/not/exist"
    spec = PluginSpec.model_validate(_spec_kwargs(migrations=value))
    assert spec.migrations == value


def test_descriptions_must_be_non_empty_single_lines():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="non-empty"):
        PluginSpec(**_spec_kwargs(description="   "))
    with pytest.raises(ValidationError, match="single line"):
        PluginSpec(**_spec_kwargs(description="two\nlines"))


def test_urls_must_be_http():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    assert PluginSpec(**_spec_kwargs(homepage="https://tai42.ai")).homepage == "https://tai42.ai"
    assert PluginSpec(**_spec_kwargs(homepage=None)).homepage is None
    with pytest.raises(ValidationError, match="http"):
        PluginSpec(**_spec_kwargs(repository="git@github.com:tai42ai/tai42.git"))


def test_license_must_be_an_spdx_id_shape():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="SPDX"):
        PluginSpec(**_spec_kwargs(license="Apache 2.0"))


@pytest.mark.parametrize(("field", "valid"), _SPEC_TOKEN_FIELDS)
def test_spec_token_fields_reject_newlines(field: str, valid: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    trailing = f"{valid}\n"
    embedded = f"{valid[:1]}\n{valid[1:]}"
    for bad in (trailing, embedded):
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(**{field: bad}))


@pytest.mark.parametrize("field", ["categories", "tags"])
def test_spec_list_token_fields_reject_newlines(field: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    for bad in ("utilities\n", "uti\nlities"):
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(**{field: [bad]}))


def test_https_icon_rejects_malformed_and_whitespace_urls():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    assert PluginSpec(**_spec_kwargs(icon="https://tai42.ai/toolbox.png")).icon == "https://tai42.ai/toolbox.png"
    # Bare scheme (no host), embedded whitespace/newline, and control chars are
    # all rejected rather than returned unchecked.
    for bad in ("https://", "https:// evil", "https://tai42.ai/x.png\n", "https://tai\t42.ai/x.png"):
        with pytest.raises(ValidationError, match="icon"):
            PluginSpec(**_spec_kwargs(icon=bad))


def test_urls_reject_malformed_and_whitespace():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    for bad in ("https://", "http:// evil", "https://tai42.ai\n", "https://ta\ti42.ai"):
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(homepage=bad))


def test_urls_reject_embedded_userinfo_and_hostless_authority():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    # ``tai42.ai@evil.com`` resolves to host evil.com behind a trusted-looking
    # authority; ``user@`` has a non-empty netloc but no host; an unterminated IPv6
    # literal makes urlsplit raise. All are rejected across icon/homepage/repository.
    for bad in ("https://tai42.ai@evil.com", "https://user@", "https://user:pass@", "https://["):
        with pytest.raises(ValidationError, match="icon"):
            PluginSpec(**_spec_kwargs(icon=bad))
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(homepage=bad))
        with pytest.raises(ValidationError):
            PluginSpec(**_spec_kwargs(repository=bad))


def test_plugins_module_imports_only_stdlib_or_pydantic():
    import ast
    import sys
    from pathlib import Path

    import tai42_contract.plugins as plugins_module

    # The contract must stay dependency-light: no third-party import (notably
    # ``packaging``) may creep back into the plugin-spec module. Re-adding one
    # would pass every behavioural test, so this AST scan is the guard. The
    # package's own root is first-party (the module reuses ``MCPConfig`` from
    # ``tai42_contract.manifest``), so it joins stdlib + pydantic.
    source_path = Path(plugins_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    allowed_roots = set(sys.stdlib_module_names) | {"pydantic", "tai42_contract"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative (same-package) import; always allowed.
            roots = [] if node.level else [(node.module or "").split(".")[0]]
        else:
            continue
        for root in roots:
            assert root in allowed_roots, f"disallowed import of {root!r} in {source_path}"


def test_http_urls_are_accepted_for_homepage_and_repository():
    from tai42_contract.plugins import PluginSpec

    # check_web_url accepts both http and https for homepage/repository; pin
    # that a plain http:// value is accepted so a narrowing to https-only fails.
    assert PluginSpec(**_spec_kwargs(homepage="http://tai42.ai")).homepage == "http://tai42.ai"
    assert (
        PluginSpec(**_spec_kwargs(repository="http://github.com/tai42ai/tai42/tree/main/plugins/toolbox")).repository
        == "http://github.com/tai42ai/tai42/tree/main/plugins/toolbox"
    )


def test_premium_defaults_false_and_round_trips():
    from tai42_contract.plugins import PluginSpec

    kwargs = _spec_kwargs()
    assert "premium" not in kwargs
    assert PluginSpec(**kwargs).premium is False
    spec = PluginSpec(**_spec_kwargs(premium=True))
    assert spec.premium is True
    # Whole-spec round-trip (the kit stores/rehydrates the dumped spec) preserves
    # the flag.
    assert PluginSpec.model_validate(spec.model_dump()).premium is True


def test_all_mcp_server_spec_omits_contract():
    from tai42_contract.plugins import PluginSpec

    spec = PluginSpec(**_all_mcp_kwargs())
    assert spec.contract is None


def test_all_mcp_server_spec_rejects_contract():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    kwargs = _all_mcp_kwargs()
    kwargs["contract"] = ">=0.1,<0.2"
    with pytest.raises(ValidationError, match="must not declare 'contract'"):
        PluginSpec(**kwargs)


def test_non_mcp_spec_requires_contract():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    kwargs = _spec_kwargs()
    del kwargs["contract"]
    with pytest.raises(ValidationError, match="'contract' is required"):
        PluginSpec(**kwargs)


def test_connector_only_spec_without_package_is_descriptor():
    from tai42_contract.plugins import PluginSpec

    spec = PluginSpec(**_connector_spec_kwargs())
    assert spec.package is None
    assert spec.delivery == "descriptor"


def test_connector_spec_with_package_is_package_delivery():
    from tai42_contract.plugins import PluginSpec

    spec = PluginSpec(**_connector_spec_kwargs(package="tai42-acme"))
    assert spec.package == "tai42-acme"
    assert spec.delivery == "package"


def test_all_mcp_server_spec_with_package_is_package_delivery():
    from tai42_contract.plugins import PluginSpec

    # ``_all_mcp_kwargs`` keeps ``_spec_kwargs``'s package (an mcp-server whose
    # command is a console script the wheel installs).
    spec = PluginSpec(**_all_mcp_kwargs())
    assert spec.package is not None
    assert spec.delivery == "package"


def test_all_mcp_server_spec_without_package_is_descriptor():
    from tai42_contract.plugins import PluginSpec

    kwargs = _all_mcp_kwargs()
    del kwargs["package"]
    spec = PluginSpec(**kwargs)
    assert spec.package is None
    assert spec.delivery == "descriptor"


def test_tool_spec_without_package_rejected():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    kwargs = _spec_kwargs()
    del kwargs["package"]
    with pytest.raises(ValidationError, match="must name its package"):
        PluginSpec(**kwargs)


def test_connector_spec_without_contract_rejected():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    kwargs = _connector_spec_kwargs()
    del kwargs["contract"]
    with pytest.raises(ValidationError, match="'contract' is required"):
        PluginSpec(**kwargs)


def test_migrations_without_package_rejected():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="migrations require a package"):
        PluginSpec(**_connector_spec_kwargs(migrations="migrations"))


def test_connector_origin_system_ok_for_tai42_namespace():
    from tai42_contract.plugins import PluginSpec

    spec = PluginSpec(**_connector_spec_kwargs())
    assert spec.namespace == "tai42"
    assert spec.provides[0].provider is not None
    assert spec.provides[0].provider.origin == "system"


def test_connector_origin_system_rejected_for_community_namespace():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError, match="must be 'system' iff"):
        PluginSpec(**_connector_spec_kwargs(namespace="iota"))


def test_connector_origin_community_rejected_for_tai42_namespace():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    item = _connector_item(provider=_connector_provider(origin="community"))
    with pytest.raises(ValidationError, match="must be 'system' iff"):
        PluginSpec(**_connector_spec_kwargs(provides=[item]))


def test_connector_origin_community_ok_for_community_namespace():
    from tai42_contract.plugins import PluginSpec

    item = _connector_item(provider=_connector_provider(origin="community"))
    spec = PluginSpec(**_connector_spec_kwargs(namespace="iota", provides=[item]))
    assert spec.provides[0].provider is not None
    assert spec.provides[0].provider.origin == "community"


def test_spec_rejects_mixed_mcp_server_and_other_kinds():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    tool_item = {
        "kind": "tool",
        "name": "generate_uuid",
        "module": "tai42_toolbox.tools.generate_uuid",
        "description": "Generate a random UUID.",
    }
    kwargs = _spec_kwargs(provides=[_mcp_item(), tool_item])
    kwargs.pop("contract", None)
    with pytest.raises(ValidationError, match="may not share a spec with other kinds"):
        PluginSpec(**kwargs)


def test_manifest_connectors_round_trip():
    from tai42_contract.connectors.providers import ProviderDescriptor
    from tai42_contract.manifest import Manifest

    provider = _connector_provider(id="iota")
    manifest = Manifest.model_validate({"connectors": [provider]})
    assert [d.id for d in manifest.connectors] == ["iota"]
    assert all(isinstance(d, ProviderDescriptor) for d in manifest.connectors)

    dumped = manifest.model_dump(mode="json")
    assert dumped["connectors"][0]["id"] == "iota"

    reloaded = Manifest.model_validate(dumped)
    assert [d.id for d in reloaded.connectors] == ["iota"]


def test_manifest_connectors_default_empty():
    from tai42_contract.manifest import Manifest

    assert Manifest().connectors == []


def test_manifest_rejects_duplicate_connector_ids():
    from pydantic import ValidationError

    from tai42_contract.manifest import Manifest

    with pytest.raises(ValidationError, match="duplicate connector id 'kappa'"):
        Manifest.model_validate({"connectors": [_connector_provider(id="kappa"), _connector_provider(id="kappa")]})
