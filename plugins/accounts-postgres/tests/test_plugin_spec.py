"""The shipped descriptors — tai-plugin.yml and the Studio bundle manifest — validate
and stay in sync with the project metadata."""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

import yaml
from tai42_contract.plugins import PluginSpec

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROOT_SPEC = _REPO_ROOT / "tai-plugin.yml"
_PACKAGED_SPEC = _REPO_ROOT / "src" / "tai42_accounts_postgres" / "tai-plugin.yml"
_STUDIO_MANIFEST = _REPO_ROOT / "src" / "tai42_accounts_postgres" / "studio" / "studio-manifest.json"


def _spec() -> PluginSpec:
    return PluginSpec.model_validate(yaml.safe_load(_ROOT_SPEC.read_text(encoding="utf-8")))


def test_plugin_spec_validates_and_names_this_listing():
    spec = _spec()
    assert spec.ref == "tai42/accounts-postgres"
    for item in spec.provides:
        assert importlib.util.find_spec(item.module) is not None, (
            f"provided item {item.kind.value}/{item.name} declares module {item.module!r}, "
            "which does not resolve to an importable module"
        )


def test_plugin_spec_matches_the_project_metadata():
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    spec = _spec()
    assert spec.package == project["name"]
    assert spec.version == project["version"]
    assert spec.description == project["description"]


def test_packaged_spec_is_declared_in_package_data():
    package_data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"][
        "package-data"
    ]
    owning = [key for key, patterns in package_data.items() if "tai-plugin.yml" in patterns]
    assert owning == ["tai42_accounts_postgres"], (
        f"exactly one package-data entry must ship tai-plugin.yml; found {owning!r} in {package_data!r}"
    )


def test_packaged_copy_is_identical_to_the_root_spec():
    assert _PACKAGED_SPEC.read_bytes() == _ROOT_SPEC.read_bytes()


def test_studio_manifest_version_matches_the_project_metadata():
    """The committed Studio bundle manifest carries the pyproject version.

    ``scripts/build-studio.mjs`` reads the version out of pyproject, so pyproject is
    the single version source; the manifest is the build's committed output. The two
    can only differ if the manifest was hand-edited or the bundle was not rebuilt
    after a version bump — either way the served plugin advertises a version its
    package does not have.
    """
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    manifest = json.loads(_STUDIO_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["version"] == project["version"], (
        f"studio-manifest.json version {manifest['version']!r} does not match the "
        f"pyproject version {project['version']!r}; rebuild the studio bundle"
    )
