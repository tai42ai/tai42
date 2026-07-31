"""tai-plugin.yml: the shipped plugin spec validates and stays in sync."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from typing import Any

import yaml
from tai42_contract.plugins import PluginSpec

_SPEC_FILENAME = "tai-plugin.yml"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROOT_SPEC = _REPO_ROOT / _SPEC_FILENAME
_PACKAGED_SPEC = _REPO_ROOT / "src" / "tai42_backend_celery" / _SPEC_FILENAME
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _spec() -> PluginSpec:
    return PluginSpec.model_validate(yaml.safe_load(_ROOT_SPEC.read_text(encoding="utf-8")))


def _pyproject() -> dict[str, Any]:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def test_plugin_spec_validates_and_names_this_listing():
    spec = _spec()
    assert spec.ref == "tai42/backend-celery"
    for item in spec.provides:
        assert importlib.util.find_spec(item.module) is not None, (
            f"provides entry {item.name!r} declares module {item.module!r}, which does not resolve"
        )


def test_plugin_spec_matches_the_project_metadata():
    project = _pyproject()["project"]
    spec = _spec()
    assert spec.package == project["name"]
    assert spec.version == project["version"]
    assert spec.description == project["description"]


def test_packaged_spec_is_declared_in_package_data():
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]
    declaring = [key for key, files in package_data.items() if _SPEC_FILENAME in files]
    assert declaring == ["tai42_backend_celery"], (
        f"{_SPEC_FILENAME!r} must be declared under exactly the owning package "
        f"'tai42_backend_celery' in [tool.setuptools.package-data], but is declared under {declaring!r}; "
        "a wrong or missing package key means the built wheel silently omits the plugin spec"
    )


def test_packaged_copy_is_identical_to_the_root_spec():
    assert _PACKAGED_SPEC.read_bytes() == _ROOT_SPEC.read_bytes()
