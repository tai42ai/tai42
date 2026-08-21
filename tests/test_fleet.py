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
from tai42_kit.plugins import load_plugin_spec, read_dir_docs, validate_docs

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


def _plugin_dirs_with(predicate) -> list[Path]:
    return [
        hit
        for hit in sorted(ROOT.glob("plugins/*"))
        if hit.is_dir() and (hit / "tai-plugin.yml").is_file() and predicate(hit)
    ]


MEMBER_DIRS = _member_dirs()
MEMBER_PATHS = [d.relative_to(ROOT).as_posix() for d in MEMBER_DIRS]

# The two disk-derived fleet sets (design point 12): a plugins/* dir with a
# pyproject.toml is a PACKAGED workspace member; a plugins/* dir carrying a
# tai-plugin.yml but NO pyproject.toml is a DESCRIPTOR-only component.
PACKAGED_DIRS = _plugin_dirs_with(lambda d: (d / "pyproject.toml").is_file())
DESCRIPTOR_DIRS = _plugin_dirs_with(lambda d: not (d / "pyproject.toml").is_file())
PACKAGED_PATHS = [d.relative_to(ROOT).as_posix() for d in PACKAGED_DIRS]
DESCRIPTOR_PATHS = [d.relative_to(ROOT).as_posix() for d in DESCRIPTOR_DIRS]

# name -> current pyproject version, over all members (for cap admission)
SIBLING_VERSIONS: dict[str, str] = {}
for _d in MEMBER_DIRS:
    _py = _load_toml(_d / "pyproject.toml")
    SIBLING_VERSIONS[_py["project"]["name"]] = _py["project"]["version"]

# The workspace contract version every descriptor's `contract` range must admit.
CONTRACT_VERSION = SIBLING_VERSIONS["tai42-contract"]


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
    packaged = list((plugin_dir / "src").rglob("tai-plugin.yml"))
    assert len(packaged) == 1, f"{plugin_dir}: expected one packaged descriptor, got {packaged}"
    return root_copy, packaged[0]


# ---------------------------------------------------------------- 1. membership


def _workspace_plugin_members() -> set[str]:
    """The plugins/* dirs uv resolves as workspace members, honouring exclude —
    the descriptor-only connector dirs are excluded because they carry no
    pyproject.toml."""
    workspace = _load_toml(ROOT / "pyproject.toml")["tool"]["uv"]["workspace"]
    exclude = set(workspace.get("exclude", []))
    resolved: set[str] = set()
    for pattern in workspace["members"]:
        for hit in glob.glob(str(ROOT / pattern)):
            p = Path(hit)
            rel = p.relative_to(ROOT).as_posix()
            if rel in exclude:
                continue
            if p.is_dir() and (p / "pyproject.toml").is_file():
                resolved.add(rel)
    return {m for m in resolved if m.startswith("plugins/")}


def test_membership_equality():
    """The packaged plugin dirs (a pyproject.toml on disk) are exactly the
    plugins uv carries as workspace members."""
    plugin_members = _workspace_plugin_members()
    assert plugin_members == set(PACKAGED_PATHS), (
        f"workspace plugin members={sorted(plugin_members)} packaged dirs={sorted(PACKAGED_PATHS)}"
    )

    for plugin_dir in PACKAGED_DIRS:
        assert (plugin_dir / "tai-plugin.yml").is_file(), f"{plugin_dir}: missing tai-plugin.yml"


# --------------------------------------------------------------- 2. cap admission


@pytest.mark.parametrize("member_dir", PACKAGED_DIRS, ids=PACKAGED_PATHS)
def test_cap_admission(member_dir: Path):
    pyproject = _load_toml(member_dir / "pyproject.toml")
    for dep_name, spec in _iter_tai42_specifiers(pyproject):
        if dep_name not in SIBLING_VERSIONS:
            continue  # not a workspace sibling
        sibling_version = SIBLING_VERSIONS[dep_name]
        assert SpecifierSet(str(spec)).contains(sibling_version, prereleases=False), (
            f"{member_dir.name}: {dep_name}{spec} does not admit sibling version {sibling_version}"
        )


# ------------------------------------------------------ 3. descriptor lockstep


@pytest.mark.parametrize("plugin_dir", PACKAGED_DIRS, ids=[d.name for d in PACKAGED_DIRS])
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
        f"{name}: descriptor contract {descriptor['contract']!r} != pyproject specifier {str(contract_spec)!r}"
    )

    assert descriptor["repository"] == f"{REPO_TREE_URL}/{member_path}"

    # packaged copy byte-identical to the root copy
    assert packaged_copy.read_bytes() == root_copy.read_bytes(), f"{name}: packaged descriptor differs from root copy"

    # both release-please extra-files paths exist on disk
    config_entry = _root_config()["packages"][member_path]
    extra_files = config_entry.get("extra-files", [])
    assert extra_files, f"{name}: plugin must declare extra-files"
    for ef in extra_files:
        assert (plugin_dir / ef["path"]).is_file(), f"{name}: extra-file missing {ef['path']}"


# ------------------------------------------------------------- 4. manifest sanity


def test_manifest_sanity():
    config_keys = set(_root_config()["packages"].keys())
    manifest = _manifest()
    manifest_keys = set(manifest.keys())

    # The release-please universe = packaged workspace members (core/*, e2e, and
    # every packaged plugin) unioned with the descriptor-only plugin dirs.
    expected = set(MEMBER_PATHS) | set(DESCRIPTOR_PATHS)
    assert config_keys == manifest_keys == expected, (
        f"config={sorted(config_keys)} manifest={sorted(manifest_keys)} expected={sorted(expected)}"
    )

    # Packaged members: the manifest never runs ahead of the built version.
    member_versions = {
        d.relative_to(ROOT).as_posix(): _load_toml(d / "pyproject.toml")["project"]["version"] for d in MEMBER_DIRS
    }
    for key, version in member_versions.items():
        assert Version(manifest[key]) <= Version(version), (
            f"manifest {key}={manifest[key]} exceeds member version {version}"
        )

    # Descriptor components: manifest == yml version == version.txt (release-please
    # `simple` bumps version.txt; the yml stays canonical — drift is loud).
    for desc_dir in DESCRIPTOR_DIRS:
        path = desc_dir.relative_to(ROOT).as_posix()
        spec = load_plugin_spec(desc_dir / "tai-plugin.yml")
        version_txt = (desc_dir / "version.txt").read_text().strip()
        assert manifest[path] == spec.version == version_txt, (
            f"{path}: manifest={manifest[path]} yml={spec.version} version.txt={version_txt}"
        )


# ---------------------------------------------------- 6. descriptor-only plugins


@pytest.mark.parametrize("plugin_dir", DESCRIPTOR_DIRS, ids=[d.name for d in DESCRIPTOR_DIRS])
def test_descriptor_only_plugins(plugin_dir: Path):
    """A descriptor-only plugin: a valid package-less spec, a first-party docs
    tree, the required sidecar files, version lockstep, and a contract range that
    admits the workspace contract version."""
    spec = load_plugin_spec(plugin_dir / "tai-plugin.yml")
    assert spec.package is None, f"{plugin_dir.name}: descriptor plugin must carry no package"

    validate_docs(read_dir_docs(plugin_dir), first_party=True)

    for sidecar in ("LICENSE", "icon.png", "CHANGELOG.md", "version.txt"):
        assert (plugin_dir / sidecar).is_file(), f"{plugin_dir.name}: missing {sidecar}"

    version_txt = (plugin_dir / "version.txt").read_text().strip()
    assert version_txt == spec.version, f"{plugin_dir.name}: version.txt={version_txt} != yml version {spec.version}"

    assert spec.contract is not None, f"{plugin_dir.name}: descriptor plugin must declare a contract range"
    assert SpecifierSet(spec.contract).contains(CONTRACT_VERSION, prereleases=True), (
        f"{plugin_dir.name}: contract {spec.contract!r} does not admit workspace contract {CONTRACT_VERSION}"
    )


# --------------------------------------------------- 5. harness API presence

HARNESS_API = {
    "tai42_e2e.stack": [
        "TaiStack",
        "Infra",
        "StackConfig",
        "StackResources",
        "Topology",
        "InfraUnavailable",
        "tai_bin",
        "uvicorn_bin",
        "spawn_expect_refusal",
    ],
    "tai42_e2e.booting": ["allocate_and_build", "boot_stack"],
    "tai42_e2e.manifests": [
        "build_replicas_stack",
        "build_accounts_stack",
        "build_studio_stack",
        "PROBE_TOOLS_TITLE",
    ],
    "tai42_e2e.harness": [
        "connect_infra",
        "allocate_resources",
        "release_resources",
        "seed_bootstrap_key",
        "seed_route_rows",
        "seed_studio_auth",
        "seed_root_identity",
    ],
    "tai42_e2e.settings": ["HarnessSettings"],
    "tai42_e2e.variants": [
        "Variants",
        "resolve_variants",
        "BusWorker",
        "bus_census",
        "BACKENDS",
        "IDENTITIES",
        "STORAGES",
        "short_presence_ttl_env",
    ],
    "tai42_e2e.waiting": ["wait_for", "wait_for_async", "WaitTimeout", "align_to_window"],
    "tai42_e2e.httpapi": ["ApiClient"],
    "tai42_e2e.llmstub": ["LlmStub"],
    "tai42_e2e.tcprelay": ["TcpRelay", "wait_relay_ready"],
    "tai42_e2e.diagnostics": ["track", "register", "unregister", "report"],
    "tai42_e2e.rabbitx": ["RabbitAdmin"],
    "tai42_e2e.pkgsource": [
        "BuiltWheel",
        "BuiltTarball",
        "FixturePackageIndex",
        "build_fixture_wheel",
        "build_fixture_source_tarball",
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
