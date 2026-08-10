"""tai-plugin.yml: the shipped plugin spec validates and stays in sync."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import yaml
from tai42_contract.plugins import PluginSpec

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROOT_SPEC = _REPO_ROOT / "tai-plugin.yml"
_PACKAGED_SPEC = _REPO_ROOT / "src" / "tai42_monitoring_langfuse" / "tai-plugin.yml"


def _spec() -> PluginSpec:
    return PluginSpec.model_validate(yaml.safe_load(_ROOT_SPEC.read_text(encoding="utf-8")))


def test_plugin_spec_validates_and_names_this_listing():
    spec = _spec()
    assert spec.ref == "tai42/monitoring-langfuse"
    for item in spec.provides:
        if item.module is not None:
            assert importlib.util.find_spec(item.module) is not None, item.module


def test_plugin_spec_matches_the_project_metadata():
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    spec = _spec()
    assert spec.package == project["name"]
    assert spec.version == project["version"]
    assert spec.description == project["description"]


def test_packaged_spec_is_declared_in_package_data():
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    shipping = [key for key, value in package_data.items() if "tai-plugin.yml" in value]
    assert shipping == ["tai42_monitoring_langfuse"]


def test_packaged_copy_is_identical_to_the_root_spec():
    assert _PACKAGED_SPEC.read_bytes() == _ROOT_SPEC.read_bytes()


def test_pyproject_ships_the_docs_tree_as_package_data():
    package_data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"][
        "package-data"
    ]
    assert "docs/*" in package_data["tai42_monitoring_langfuse"], (
        "the in-package docs/ tree must ship via the 'docs/*' glob on the "
        "'tai42_monitoring_langfuse' [tool.setuptools.package-data] key, or the wheel omits docs/index.mdx"
    )
