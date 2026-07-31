"""Root fleet-consistency test — cross-package invariants the per-package
suites cannot see. Each package keeps its own in-package descriptor lockstep
test; this asserts the fleet-wide equalities."""

from __future__ import annotations

import glob
import json
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
REPO_TREE_URL = "https://github.com/tai42ai/tai42/tree/main"

WORKSPACE_GLOBS = ["core/*", "plugins/*", "e2e"]


def _load_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _member_dirs() -> list[Path]:
    dirs: list[Path] = []
    for pattern in WORKSPACE_GLOBS:
        for hit in sorted(ROOT.glob(pattern)):
            if hit.is_dir() and (hit / "pyproject.toml").is_file():
                dirs.append(hit)
    return dirs


MEMBER_DIRS = _member_dirs()
MEMBER_PATHS = [d.relative_to(ROOT).as_posix() for d in MEMBER_DIRS]
PLUGIN_DIRS = [d for d in MEMBER_DIRS if d.relative_to(ROOT).parts[0] == "plugins"]

# name -> current pyproject version, over all members (for cap admission)
SIBLING_VERSIONS: dict[str, str] = {}
for _d in MEMBER_DIRS:
    _py = _load_toml(_d / "pyproject.toml")
    SIBLING_VERSIONS[_py["project"]["name"]] = _py["project"]["version"]


def _root_config() -> dict:
    return json.loads((ROOT / "release-please-config.json").read_text())


def _manifest() -> dict:
    return json.loads((ROOT / ".release-please-manifest.json").read_text())


def _iter_tai42_specifiers(pyproject: dict):
    """Yield (dep_name, SpecifierSet) for every tai42-* requirement across
    dependencies, optional-dependencies, and dependency-groups."""
    from packaging.requirements import Requirement

    project = pyproject.get("project", {})
    buckets: list[str] = list(project.get("dependencies", []))
    for extra_deps in project.get("optional-dependencies", {}).values():
        buckets.extend(extra_deps)
    for group_deps in pyproject.get("dependency-groups", {}).values():
        for item in group_deps:
            if isinstance(item, str):
                buckets.append(item)
    for raw in buckets:
        try:
            req = Requirement(raw)
        except Exception:
            continue
        if req.name.startswith("tai42-"):
            yield req.name, req.specifier


def _plugin_descriptor(plugin_dir: Path) -> tuple[Path, Path]:
    """Return (root_copy, packaged_copy) descriptor paths for a plugin."""
    root_copy = plugin_dir / "tai-plugin.yml"
    packaged = [p for p in (plugin_dir / "src").rglob("tai-plugin.yml")]
    assert len(packaged) == 1, f"{plugin_dir}: expected one packaged descriptor, got {packaged}"
    return root_copy, packaged[0]


# ---------------------------------------------------------------- 1. membership

def test_membership_equality():
    workspace_members = _load_toml(ROOT / "pyproject.toml")["tool"]["uv"]["workspace"]["members"]
    resolved: set[str] = set()
    for pattern in workspace_members:
        for hit in glob.glob(str(ROOT / pattern)):
            p = Path(hit)
            if p.is_dir() and (p / "pyproject.toml").is_file():
                resolved.add(p.relative_to(ROOT).as_posix())

    config_paths = set(_root_config()["packages"].keys())
    dir_paths = set(MEMBER_PATHS)

    assert resolved == config_paths == dir_paths, (
        f"members glob={sorted(resolved)} "
        f"config={sorted(config_paths)} dirs={sorted(dir_paths)}"
    )

    for plugin_dir in PLUGIN_DIRS:
        assert (plugin_dir / "tai-plugin.yml").is_file(), f"{plugin_dir}: missing tai-plugin.yml"


def test_connector_namespace_has_no_init():
    """tai42_connector is an implicit namespace shared by two members; an
    __init__.py in either would break the other."""
    for member in ("plugins/connector-atlassian", "plugins/connector-google"):
        init = ROOT / member / "src" / "tai42_connector" / "__init__.py"
        assert not init.exists(), f"{init} must not exist (implicit namespace)"


# --------------------------------------------------------------- 2. cap admission

@pytest.mark.parametrize("member_dir", MEMBER_DIRS, ids=MEMBER_PATHS)
def test_cap_admission(member_dir: Path):
    pyproject = _load_toml(member_dir / "pyproject.toml")
    for dep_name, spec in _iter_tai42_specifiers(pyproject):
        if dep_name not in SIBLING_VERSIONS:
            continue  # not a workspace sibling
        sibling_version = SIBLING_VERSIONS[dep_name]
        assert SpecifierSet(str(spec)).contains(sibling_version, prereleases=False), (
            f"{member_dir.name}: {dep_name}{spec} does not admit "
            f"sibling version {sibling_version}"
        )


# ------------------------------------------------------ 3. descriptor lockstep

@pytest.mark.parametrize("plugin_dir", PLUGIN_DIRS, ids=[d.name for d in PLUGIN_DIRS])
def test_descriptor_lockstep(plugin_dir: Path):
    pyproject = _load_toml(plugin_dir / "pyproject.toml")
    name = pyproject["project"]["name"]
    version = pyproject["project"]["version"]
    member_path = plugin_dir.relative_to(ROOT).as_posix()

    root_copy, packaged_copy = _plugin_descriptor(plugin_dir)
    descriptor = yaml.safe_load(root_copy.read_text())

    assert descriptor["package"] == name
    assert str(descriptor["version"]) == version

    # descriptor contract specifier == the pyproject tai42-contract specifier
    # (set equality — specifier ordering is not semantic)
    contract_spec = None
    for dep_name, spec in _iter_tai42_specifiers(pyproject):
        if dep_name == "tai42-contract":
            contract_spec = spec
            break
    assert contract_spec is not None, f"{name}: no tai42-contract dependency"
    assert SpecifierSet(str(descriptor["contract"])) == contract_spec, (
        f"{name}: descriptor contract {descriptor['contract']!r} != "
        f"pyproject specifier {str(contract_spec)!r}"
    )

    assert descriptor["repository"] == f"{REPO_TREE_URL}/{member_path}"

    # packaged copy byte-identical to the root copy
    assert packaged_copy.read_bytes() == root_copy.read_bytes(), (
        f"{name}: packaged descriptor differs from root copy"
    )

    # both release-please extra-files paths exist on disk
    config_entry = _root_config()["packages"][member_path]
    extra_files = config_entry.get("extra-files", [])
    assert extra_files, f"{name}: plugin must declare extra-files"
    for ef in extra_files:
        assert (plugin_dir / ef["path"]).is_file(), f"{name}: extra-file missing {ef['path']}"


# ------------------------------------------------------------- 4. manifest sanity

def test_manifest_sanity():
    manifest = _manifest()
    member_versions = {
        d.relative_to(ROOT).as_posix(): _load_toml(d / "pyproject.toml")["project"]["version"]
        for d in MEMBER_DIRS
    }
    for key, value in manifest.items():
        assert key in member_versions, f"manifest key {key} is not a member path"
        assert Version(value) <= Version(member_versions[key]), (
            f"manifest {key}={value} exceeds member version {member_versions[key]}"
        )


# --------------------------------------------------- 5. harness API presence

HARNESS_API = {
    "tai42_e2e.stack": [
        "TaiStack", "Infra", "StackConfig", "StackResources", "Topology",
        "InfraUnavailable", "tai_bin", "uvicorn_bin", "spawn_expect_refusal",
    ],
    "tai42_e2e.booting": ["allocate_and_build", "boot_stack"],
    "tai42_e2e.manifests": [
        "build_replicas_stack", "build_accounts_stack", "build_studio_stack",
        "PROBE_TOOLS_TITLE",
    ],
    "tai42_e2e.harness": [
        "connect_infra", "allocate_resources", "release_resources",
        "seed_bootstrap_key", "seed_route_rows", "seed_studio_auth",
        "seed_root_identity",
    ],
    "tai42_e2e.settings": ["HarnessSettings"],
    "tai42_e2e.variants": [
        "Variants", "resolve_variants", "BusOrigin", "bus_census", "BACKENDS",
        "IDENTITIES", "STORAGES", "short_presence_ttl_env",
    ],
    "tai42_e2e.waiting": ["wait_for", "wait_for_async", "WaitTimeout", "align_to_window"],
    "tai42_e2e.httpapi": ["ApiClient"],
    "tai42_e2e.llmstub": ["LlmStub"],
    "tai42_e2e.tcprelay": ["TcpRelay", "wait_relay_ready"],
    "tai42_e2e.diagnostics": ["track", "register", "unregister", "report"],
    "tai42_e2e.rabbitx": ["RabbitAdmin"],
    "tai42_e2e.pkgsource": [
        "BuiltWheel", "BuiltTarball", "FixturePackageIndex",
        "build_fixture_wheel", "build_fixture_source_tarball",
    ],
    "tai42_e2e.pytest_plugin": [],
}


def test_harness_public_api_present():
    pytest.importorskip("tai42_e2e", reason="tai42-e2e not installed in this venv")
    import importlib

    for module_name, names in HARNESS_API.items():
        module = importlib.import_module(module_name)
        for name in names:
            assert hasattr(module, name), f"{module_name}.{name} missing from public API"
