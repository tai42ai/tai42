"""``tai plugins init`` / ``schema`` — local scaffolding, no server.

The scaffolded tree must parse as a plugin spec and its docs must satisfy the
front-matter contract as-is. ``tai42_kit`` (which owns ``load_plugin_spec`` /
``validate_docs``) is walled out of this client and is not installed in its
scoped env, so the invariants are checked here against the ``tai42_contract``
``PluginSpec`` model the loader validates with, plus the same front-matter shape
the docs validator enforces.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

import yaml
from click.testing import CliRunner
from tai42_contract.plugins import PluginSpec

from tai42_cli import app as app_module

_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _run(args: list[str]):
    return CliRunner().invoke(app_module.app, args)


def _load_spec(plugin_dir: Path) -> PluginSpec:
    """Replicate ``load_plugin_spec``: parse the yml and validate the model."""
    raw = yaml.safe_load((plugin_dir / "tai-plugin.yml").read_text())
    return PluginSpec.model_validate(raw)


def _assert_docs_front_matter(plugin_dir: Path) -> None:
    """The docs contract's core: a leading ``---`` block that is exactly a
    ``title`` + ``description`` mapping of single-line strings."""
    text = (plugin_dir / "docs" / "index.mdx").read_text()
    match = _FRONT_MATTER_RE.match(text)
    assert match is not None, "docs/index.mdx must open with a front-matter block"
    meta = yaml.safe_load(match.group(1))
    assert set(meta) == {"title", "description"}, meta
    for value in meta.values():
        assert isinstance(value, str)
        assert value
        assert "\n" not in value


def test_init_connector_oauth_tree_parses_and_docs_validate(tmp_path: Path) -> None:
    dest = tmp_path / "connector-acme"
    result = _run(["plugins", "init", "connector-acme", "--kind", "connector", "--auth", "oauth", "--dir", str(dest)])
    assert result.exit_code == 0, result.output
    spec = _load_spec(dest)
    assert spec.name == "connector-acme"
    assert spec.package is None
    assert spec.delivery == "descriptor"
    assert [item.kind.value for item in spec.provides] == ["connector"]
    provider = spec.provides[0].provider
    assert provider is not None
    assert provider.kind == "oauth"
    _assert_docs_front_matter(dest)
    # The whole descriptor-only tree is scaffolded.
    for name in ("README.md", "CHANGELOG.md", "LICENSE", "NOTICE", "version.txt"):
        assert (dest / name).is_file(), name
    assert (dest / "version.txt").read_text().strip() == spec.version


def test_init_connector_none_carries_config_fields_not_oauth(tmp_path: Path) -> None:
    dest = tmp_path / "connector-kappa"
    result = _run(["plugins", "init", "connector-kappa", "--kind", "connector", "--auth", "none", "--dir", str(dest)])
    assert result.exit_code == 0, result.output
    spec = _load_spec(dest)
    provider = spec.provides[0].provider
    assert provider is not None
    assert provider.kind == "none"
    assert provider.oauth is None
    assert provider.config_fields  # no-auth template ships config_fields
    _assert_docs_front_matter(dest)


def test_init_mcp_server_is_descriptor_only_no_contract(tmp_path: Path) -> None:
    dest = tmp_path / "mcp-relay"
    result = _run(["plugins", "init", "mcp-relay", "--kind", "mcp-server", "--dir", str(dest)])
    assert result.exit_code == 0, result.output
    spec = _load_spec(dest)
    assert spec.name == "mcp-relay"
    assert spec.package is None
    assert spec.contract is None  # an all-mcp-server spec declares no contract
    item = spec.provides[0]
    assert item.kind.value == "mcp-server"
    assert item.mcp is not None
    assert item.mcp.type == "http"
    assert item.module is None
    _assert_docs_front_matter(dest)


def test_init_connector_requires_auth(tmp_path: Path) -> None:
    result = _run(["plugins", "init", "iota", "--kind", "connector", "--dir", str(tmp_path / "iota")])
    assert result.exit_code != 0
    assert "auth" in result.output


def test_init_mcp_server_forbids_auth(tmp_path: Path) -> None:
    result = _run(["plugins", "init", "relay", "--kind", "mcp-server", "--auth", "oauth", "--dir", str(tmp_path / "r")])
    assert result.exit_code != 0
    assert "auth" in result.output


def test_init_rejects_unknown_kind(tmp_path: Path) -> None:
    result = _run(["plugins", "init", "iota", "--kind", "tool", "--dir", str(tmp_path / "iota")])
    assert result.exit_code != 0
    assert "kind" in result.output


def test_init_rejects_bad_name(tmp_path: Path) -> None:
    result = _run(["plugins", "init", "Bad_Name", "--kind", "mcp-server", "--dir", str(tmp_path / "x")])
    assert result.exit_code != 0
    assert "NAME" in result.output


def test_init_refuses_existing_dir(tmp_path: Path) -> None:
    dest = tmp_path / "taken"
    dest.mkdir()
    result = _run(["plugins", "init", "taken", "--kind", "mcp-server", "--dir", str(dest)])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_schema_is_valid_json_with_provides() -> None:
    result = _run(["plugins", "schema"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert "provides" in parsed["properties"]


def test_templates_are_packaged() -> None:
    """The three skeletons ship as package data (importlib.resources sees them),
    so the wheel carries them and ``init`` never reads off the source tree."""
    root = resources.files("tai42_cli") / "templates"
    for template in ("connector-oauth", "connector-none", "mcp-server"):
        base = root / template
        assert (base / "tai-plugin.yml").is_file()
        assert (base / "docs" / "index.mdx").is_file()
        for name in ("README.md", "CHANGELOG.md", "LICENSE", "NOTICE", "version.txt"):
            assert (base / name).is_file(), (template, name)
