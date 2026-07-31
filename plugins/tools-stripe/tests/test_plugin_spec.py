"""tai-plugin.yml: the shipped plugin spec validates and stays in sync."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import yaml
from tai42_contract.plugins import PluginSpec

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROOT_SPEC = _REPO_ROOT / "tai-plugin.yml"
_PACKAGED_SPEC = _REPO_ROOT / "src" / "tai42_tools_stripe" / "tai-plugin.yml"


def _spec() -> PluginSpec:
    return PluginSpec.model_validate(yaml.safe_load(_ROOT_SPEC.read_text(encoding="utf-8")))


def test_plugin_spec_validates_and_names_this_listing():
    spec = _spec()
    assert spec.ref == "tai42/tools-stripe"
    for item in spec.provides:
        assert importlib.util.find_spec(item.module) is not None, f"declared module does not resolve: {item.module}"


def test_plugin_spec_matches_the_project_metadata():
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    spec = _spec()
    assert spec.package == project["name"]
    assert spec.version == project["version"]
    assert spec.description == project["description"]
    # The yml's contract range and pyproject's tai42-contract specifier must be
    # the same string: the marketplace validates installs against the yml while
    # pip resolves pyproject, so drift makes the package uninstallable.
    (contract_requirement,) = [dep for dep in project["dependencies"] if dep.startswith("tai42-contract")]
    assert spec.contract == contract_requirement.removeprefix("tai42-contract")


def test_packaged_copy_is_declared_in_package_data():
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    owning = [key for key, patterns in package_data.items() if "tai-plugin.yml" in patterns]
    assert owning == ["tai42_tools_stripe"], (
        "tai-plugin.yml must be declared in exactly one [tool.setuptools.package-data] entry "
        f"so the wheel ships it; found owners {owning}"
    )


def test_packaged_copy_is_identical_to_the_root_spec():
    assert _PACKAGED_SPEC.read_bytes() == _ROOT_SPEC.read_bytes()
