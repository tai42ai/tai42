"""Tests for ``PluginItem`` — the item fields (name/module/description/tags/group) and the
table-driven shape-by-kind rules (module vs data-block, connector/mcp-server, foreign blocks)."""

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


def test_item_kind_must_be_known():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    with pytest.raises(ValidationError):
        PluginItem(kind="gizmo", name="x", module="a.b", description="d")  # pyright: ignore[reportArgumentType]


def test_item_module_must_be_a_dotted_import_path():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    ok = PluginItem(kind=PluginItemKind.TOOL, name="x", module="pkg.sub.mod", description="d")
    assert ok.module == "pkg.sub.mod"
    for bad in ("pkg..mod", ".pkg", "pkg.", "pkg-name", "1pkg"):
        with pytest.raises(ValidationError, match="import path"):
            PluginItem(kind=PluginItemKind.TOOL, name="x", module=bad, description="d")


def test_item_name_must_be_a_registration_slug():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    with pytest.raises(ValidationError, match="item name"):
        PluginItem(kind=PluginItemKind.TOOL, name="Bad Name", module="a.b", description="d")


def test_item_description_must_be_a_non_empty_single_line():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    with pytest.raises(ValidationError, match="single line"):
        PluginItem(kind=PluginItemKind.TOOL, name="x", module="a.b", description="two\nlines")


def test_item_tags_are_bounded():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    with pytest.raises(ValidationError, match="at most 10"):
        PluginItem(
            kind=PluginItemKind.TOOL,
            name="x",
            module="a.b",
            description="d",
            tags=[f"t{i}" for i in range(11)],
        )


def test_item_group_is_optional_and_defaults_to_none():
    from tai42_contract.plugins import PluginItem, PluginItemKind

    # A standalone item declares no family: the field is absent (base kwargs omit
    # it) and also accepts an explicit None.
    item = PluginItem(kind=PluginItemKind.TOOL, name="x", module="a.b", description="d")
    assert item.group is None
    assert PluginItem(kind=PluginItemKind.TOOL, name="x", module="a.b", description="d", group=None).group is None


@pytest.mark.parametrize("value", ["core", "web-tools", "group_1", "0"])
def test_item_group_accepts_tag_shaped_labels(value: str):
    from tai42_contract.plugins import PluginItem, PluginItemKind

    item = PluginItem(kind=PluginItemKind.TOOL, name="x", module="a.b", description="d", group=value)
    assert item.group == value


def test_item_group_rejects_malformed_labels():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    for bad in ("Core", "-lead", "with space", "core\n", "co\nre", ""):
        with pytest.raises(ValidationError, match="group"):
            PluginItem(kind=PluginItemKind.TOOL, name="x", module="a.b", description="d", group=bad)


def test_provides_accepts_shared_cross_kind_and_single_member_groups():
    from tai42_contract.plugins import PluginSpec

    # Two items may share one group, a group may span kinds, and a group may have
    # a single member — every arrangement is legal structural self-description.
    provides = [
        {
            "kind": "tool",
            "name": "generate_uuid",
            "module": "tai42_toolbox.tools.generate_uuid",
            "description": "Generate a random UUID.",
            "group": "core",
        },
        {
            "kind": "tool",
            "name": "parse_uuid",
            "module": "tai42_toolbox.tools.parse_uuid",
            "description": "Parse a UUID.",
            "group": "core",
        },
        {
            "kind": "agent",
            "name": "uuid_agent",
            "module": "tai42_toolbox.agents.uuid_agent",
            "description": "An agent over the UUID tools.",
            "group": "core",
        },
        {
            "kind": "tool",
            "name": "flip_coin",
            "module": "tai42_toolbox.tools.flip_coin",
            "description": "Flip a coin.",
            "group": "random",
        },
    ]
    spec = PluginSpec(**_spec_kwargs(provides=provides))
    groups = [item.group for item in spec.provides]
    assert groups == ["core", "core", "core", "random"]


def test_plugin_item_rejects_unknown_key():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    # PluginItem is extra='forbid': a stray key is a loud reject, not ignored.
    with pytest.raises(ValidationError, match="weight"):
        PluginItem(
            kind=PluginItemKind.TOOL,
            name="x",
            module="a.b",
            description="d",
            weight=3,  # pyright: ignore[reportCallIssue]
        )


def test_item_name_and_module_reject_newlines():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    for bad in ("generate_uuid\n", "gen\nerate"):
        with pytest.raises(ValidationError, match="item name"):
            PluginItem(kind=PluginItemKind.TOOL, name=bad, module="a.b", description="d")
    for bad in ("a.b\n", "a\n.b"):
        with pytest.raises(ValidationError, match="import path"):
            PluginItem(kind=PluginItemKind.TOOL, name="x", module=bad, description="d")


def test_item_tags_reject_newlines():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    for bad in ("uuid\n", "uu\nid"):
        with pytest.raises(ValidationError, match="tag"):
            PluginItem(kind=PluginItemKind.TOOL, name="x", module="a.b", description="d", tags=[bad])


def test_mcp_server_item_takes_mcp_and_no_module():
    from tai42_contract.manifest import MCPConfig
    from tai42_contract.plugins import PluginItem, PluginItemKind

    item = PluginItem(**_mcp_item())
    assert item.kind is PluginItemKind.MCP_SERVER
    assert item.module is None
    assert isinstance(item.mcp, MCPConfig)
    assert item.mcp.url == "https://mcp.example.com"


def test_mcp_server_item_rejects_module():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    with pytest.raises(ValidationError, match="must not set 'module'"):
        PluginItem(**_mcp_item(module="pkg.mod"))


def test_mcp_server_item_requires_mcp():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    item = _mcp_item()
    del item["mcp"]
    with pytest.raises(ValidationError, match="requires 'mcp'"):
        PluginItem(**item)


def test_mcp_server_item_requires_a_transport():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    # An mcp-server with an empty/transportless mcp (url/uds/command all None) is
    # an unusable shell — MCPConfig permits zero-transport only for runtime
    # mutate-later entries, so a static spec is rejected loudly at the plugin
    # boundary rather than surfacing late at spawn.
    with pytest.raises(ValidationError, match="must declare a transport"):
        PluginItem(**_mcp_item(mcp={}))


def test_non_mcp_item_requires_module():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    with pytest.raises(ValidationError, match="requires 'module'"):
        PluginItem(kind=PluginItemKind.TOOL, name="x", description="d")


def test_non_mcp_item_rejects_mcp():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    with pytest.raises(ValidationError, match="must not set 'mcp'"):
        PluginItem(
            kind=PluginItemKind.TOOL,
            name="x",
            module="a.b",
            mcp={"url": "https://mcp.example.com"},  # pyright: ignore[reportArgumentType]
            description="d",
        )


def test_connector_item_takes_provider_and_no_module():
    from tai42_contract.connectors.providers import ProviderDescriptor
    from tai42_contract.plugins import PluginItem, PluginItemKind

    item = PluginItem(**_connector_item())
    assert item.kind is PluginItemKind.CONNECTOR
    assert item.module is None
    assert isinstance(item.provider, ProviderDescriptor)
    assert item.provider.id == "acme"


def test_connector_item_rejects_module():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    with pytest.raises(ValidationError, match="must not set 'module'"):
        PluginItem(**_connector_item(module="pkg.mod"))


def test_connector_item_name_must_equal_provider_id():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    with pytest.raises(ValidationError, match=r"must equal provider\.id"):
        PluginItem(**_connector_item(name="other"))


def test_connector_item_rejects_foreign_data_block():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    # A connector item carries only its own ``provider`` block; another kind's
    # data block (``mcp``) is a loud reject.
    with pytest.raises(ValidationError, match="must not set 'mcp'"):
        PluginItem(**_connector_item(mcp={"url": "https://mcp.example.com"}))


def test_mcp_server_item_rejects_foreign_data_block():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem

    # Symmetric: an mcp-server item must not carry a connector ``provider``.
    with pytest.raises(ValidationError, match="must not set 'provider'"):
        PluginItem(**_mcp_item(provider=_connector_provider()))


def test_tool_item_rejects_provider():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginItem, PluginItemKind

    with pytest.raises(ValidationError, match="must not set 'provider'"):
        PluginItem(
            kind=PluginItemKind.TOOL,
            name="x",
            module="a.b",
            provider=_connector_provider(),  # pyright: ignore[reportArgumentType]
            description="d",
        )
