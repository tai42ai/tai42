"""Env-requirement derivation and the on-disk docs reader.

``required_env`` is the one chokepoint every install surface reads to learn which
environment variables a plugin item needs; ``read_dir_docs`` is the descriptor
(no-wheel) counterpart of ``read_wheel_docs``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tai42_contract.connectors.providers import (
    McpServerDescriptor,
    OAuthEndpoints,
    ProviderDescriptor,
    SubServiceDescriptor,
)
from tai42_contract.manifest import MCPConfig
from tai42_contract.plugins import PluginItem, PluginItemKind, PluginSpec

from tai42_kit.plugins import (
    MAX_DOCS_FILE_BYTES,
    EnvRequirement,
    PluginDocsError,
    accepts_env,
    read_dir_docs,
    required_env,
    required_env_for_spec,
)


def _mcp_item(name: str, mcp: MCPConfig) -> PluginItem:
    return PluginItem(kind=PluginItemKind.MCP_SERVER, name=name, mcp=mcp, description="An MCP server.")


def _oauth_provider(
    provider_id: str = "acme",
    *,
    client_id_env: str = "ACME_CLIENT_ID",
    client_secret_env: str = "ACME_CLIENT_SECRET",
) -> ProviderDescriptor:
    return ProviderDescriptor(
        id=provider_id,
        display_name="Acme",
        icon_url="https://cdn.example/acme.png",
        kind="oauth",
        origin="community",
        category="productivity",
        oauth=OAuthEndpoints(authorize="https://acme.example/authorize", token="https://acme.example/token"),
        client_id_env=client_id_env,
        client_secret_env=client_secret_env,
        sub_services={
            "mail": SubServiceDescriptor(
                id="mail",
                display_name="Mail",
                scopes=["mail.read"],
                mcp_server=McpServerDescriptor(type="http", url="https://acme.example/mcp"),
            )
        },
    )


def _none_provider(provider_id: str = "iota") -> ProviderDescriptor:
    return ProviderDescriptor(
        id=provider_id,
        display_name="Iota",
        icon_url="https://cdn.example/iota.png",
        kind="none",
        origin="community",
        category="productivity",
        sub_services={
            "hub": SubServiceDescriptor(
                id="hub",
                display_name="Hub",
                mcp_server=McpServerDescriptor(type="stdio", command="iota-hub"),
            )
        },
    )


def _connector_item(provider: ProviderDescriptor) -> PluginItem:
    return PluginItem(kind=PluginItemKind.CONNECTOR, name=provider.id, provider=provider, description="A connector.")


def _tool_item(name: str = "generate_uuid") -> PluginItem:
    return PluginItem(kind=PluginItemKind.TOOL, name=name, module="acme.tools.generate_uuid", description="A tool.")


# --- required_env: mcp-server markers -----------------------------------------


def test_required_env_mcp_env_markers_keeps_required_only():
    # A bare ``${VAR}`` marker is a required var; a ``${VAR:default}`` is optional
    # and contributes nothing. Both live in the launcher ``env`` block.
    item = _mcp_item(
        "relay",
        MCPConfig(
            command="relay",
            env={"TOKEN": "!ENV ${RELAY_TOKEN}", "REGION": "!ENV ${RELAY_REGION:us}"},
        ),
    )
    assert required_env(item) == (EnvRequirement(name="RELAY_TOKEN", secret=False),)


def test_required_env_mcp_header_markers_are_non_secret():
    # Markers in the HTTP ``headers`` block derive requirements too; a spec cannot
    # know a value is sensitive, so every marker-derived requirement is non-secret.
    item = _mcp_item(
        "relay",
        MCPConfig(url="https://relay.example/mcp", headers={"Authorization": "!ENV ${RELAY_KEY}"}),
    )
    assert required_env(item) == (EnvRequirement(name="RELAY_KEY", secret=False),)


def test_required_env_mcp_dedupes_first_seen():
    item = _mcp_item(
        "relay",
        MCPConfig(
            command="relay",
            env={"A": "!ENV ${RELAY_TOKEN}", "B": "!ENV ${RELAY_TOKEN}"},
        ),
    )
    assert required_env(item) == (EnvRequirement(name="RELAY_TOKEN", secret=False),)


def test_required_env_mcp_without_markers_is_empty():
    assert required_env(_mcp_item("relay", MCPConfig(url="https://relay.example/mcp"))) == ()


# --- required_env: connectors --------------------------------------------------


def test_required_env_oauth_connector_returns_client_pair():
    item = _connector_item(_oauth_provider())
    assert required_env(item) == (
        EnvRequirement(name="ACME_CLIENT_ID", secret=False),
        EnvRequirement(name="ACME_CLIENT_SECRET", secret=True),
    )


def test_required_env_none_connector_is_empty():
    # A no-auth provider's config_fields are supplied by the end user at connect
    # time, so it requires nothing at install.
    assert required_env(_connector_item(_none_provider())) == ()


def test_required_env_tool_is_empty():
    assert required_env(_tool_item()) == ()


# --- required_env_for_spec: ordered union -------------------------------------


def _spec(*items: PluginItem, package: str | None = None, contract: str | None = ">=2,<3") -> PluginSpec:
    # Every provider fixture is community origin, so the listing namespace stays
    # community too (system origin is the only one tied to the ``tai42`` namespace).
    return PluginSpec(
        spec_version=1,
        namespace="acme",
        name="bundle",
        package=package,
        version="0.1.0",
        description="A bundle.",
        license="Apache-2.0",
        contract=contract,
        categories=["productivity"],
        provides=list(items),
    )


def test_required_env_for_spec_unions_in_first_seen_order_secret_wins():
    # A var contributed non-secret by one item and secret by another resolves to
    # secret=True while keeping its first-seen position. ``SHARED`` is the
    # non-secret client_id of the first connector and the secret client_secret of
    # the second, so the union must mask it (secret=True) at position 0.
    first = _connector_item(_oauth_provider("acme", client_id_env="SHARED"))
    second = _connector_item(_oauth_provider("kappa", client_id_env="KAPPA_CLIENT_ID", client_secret_env="SHARED"))
    spec = _spec(first, second)
    assert required_env_for_spec(spec) == (
        EnvRequirement(name="SHARED", secret=True),
        EnvRequirement(name="ACME_CLIENT_SECRET", secret=True),
        EnvRequirement(name="KAPPA_CLIENT_ID", secret=False),
    )


def test_required_env_for_spec_connector_pair_order():
    spec = _spec(_connector_item(_oauth_provider("acme")))
    assert required_env_for_spec(spec) == (
        EnvRequirement(name="ACME_CLIENT_ID", secret=False),
        EnvRequirement(name="ACME_CLIENT_SECRET", secret=True),
    )


# --- accepts_env ---------------------------------------------------------------


def test_accepts_env_true_for_defaulted_only_markers():
    # A defaulted marker yields no required_env, yet the installer still offers an
    # env step so an operator can override the default.
    item = _mcp_item("relay", MCPConfig(command="relay", env={"R": "!ENV ${RELAY_REGION:us}"}))
    assert accepts_env(_spec(item, contract=None)) is True


def test_accepts_env_false_for_tool_without_markers():
    # A tool is a code/module item, so its spec must name a package; with no env
    # markers the installer offers no env step.
    assert accepts_env(_spec(_tool_item(), package="acme-tools")) is False


def test_accepts_env_true_for_oauth_connector():
    assert accepts_env(_spec(_connector_item(_oauth_provider()))) is True


def test_accepts_env_false_for_none_connector():
    assert accepts_env(_spec(_connector_item(_none_provider()))) is False


# --- read_dir_docs -------------------------------------------------------------

_MINIMAL_INDEX = """\
---
title: Home
description: The landing page.
---

# Home
"""


def _write_docs_dir(base: Path, entries: dict[str, str | bytes]) -> Path:
    for relative, content in entries.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return base


def test_read_dir_docs_reads_the_on_disk_tree(tmp_path: Path):
    _write_docs_dir(
        tmp_path,
        {
            "docs/index.mdx": _MINIMAL_INDEX,
            "docs/images/logo.png": b"\x89PNG\r\n\x1a\n binary",
        },
    )
    docs = read_dir_docs(tmp_path)
    assert set(docs) == {"docs/index.mdx", "docs/images/logo.png"}
    assert docs["docs/index.mdx"] == _MINIMAL_INDEX.encode()


def test_read_dir_docs_missing_tree_raises(tmp_path: Path):
    with pytest.raises(PluginDocsError, match="no docs/ directory"):
        read_dir_docs(tmp_path)


def test_read_dir_docs_empty_tree_raises(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    with pytest.raises(PluginDocsError, match="packages no docs/ tree"):
        read_dir_docs(tmp_path)


def test_read_dir_docs_per_file_ceiling_raises(tmp_path: Path):
    _write_docs_dir(
        tmp_path,
        {
            "docs/index.mdx": _MINIMAL_INDEX,
            "docs/big.mdx": "x" * (MAX_DOCS_FILE_BYTES + 1),
        },
    )
    with pytest.raises(PluginDocsError, match="per-file limit"):
        read_dir_docs(tmp_path)


def test_read_dir_docs_symlink_escape_raises(tmp_path: Path):
    # A docs member that is a symlink resolving outside the docs tree would pull
    # external bytes into the map, so the reader refuses it rather than following.
    outside = tmp_path / "outside.mdx"
    outside.write_text(_MINIMAL_INDEX, encoding="utf-8")
    plugin_dir = tmp_path / "plugin"
    docs = plugin_dir / "docs"
    docs.mkdir(parents=True)
    (docs / "index.mdx").symlink_to(outside)
    with pytest.raises(PluginDocsError, match="resolves outside"):
        read_dir_docs(plugin_dir)


def test_read_dir_docs_symlinked_root_raises(tmp_path: Path):
    # ``docs/`` itself being a symlink to an external tree would make every file
    # under it look contained, so the reader requires a real directory inside the
    # plugin dir rather than following a symlinked root.
    external = tmp_path / "extdocs"
    external.mkdir()
    (external / "index.mdx").write_text(_MINIMAL_INDEX, encoding="utf-8")
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "docs").symlink_to(external, target_is_directory=True)
    with pytest.raises(PluginDocsError, match="is not a real docs/ directory"):
        read_dir_docs(plugin_dir)


def test_read_dir_docs_symlinked_root_inside_plugin_dir_raises(tmp_path: Path):
    # ``docs/`` symlinked to another directory INSIDE the plugin dir resolves to a
    # contained real path, so the member check alone would pass; the reader still
    # refuses it because a symlinked root is never a real docs/ directory.
    plugin_dir = tmp_path / "plugin"
    other = plugin_dir / "otherdir"
    other.mkdir(parents=True)
    (other / "index.mdx").write_text(_MINIMAL_INDEX, encoding="utf-8")
    (plugin_dir / "docs").symlink_to(other, target_is_directory=True)
    with pytest.raises(PluginDocsError, match="is not a real docs/ directory"):
        read_dir_docs(plugin_dir)
