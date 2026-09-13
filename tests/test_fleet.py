"""Root fleet-consistency test — cross-package invariants the per-package
suites cannot see. Each package keeps its own in-package descriptor lockstep
test; this asserts the fleet-wide equalities."""

from __future__ import annotations

import glob
import json
import os
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from tai42_kit.plugins import load_plugin_spec, read_dir_docs, validate_docs

import range_sync  # importable via the scripts/ path tests/conftest.py injects

ROOT = Path(__file__).resolve().parent.parent
REPO_TREE_URL = "https://github.com/tai42ai/tai42/tree/main"

# A release-please train bumps a depended-upon member ahead of its dependents'
# not-yet-re-pinned ranges; the post-merge `fix(deps)` re-pin (release-repin.yml)
# lands one commit later. On a development PR the on-disk ranges are asserted
# as-is, so a hand-authored range that refuses a sibling is caught. On the release
# train branch and on the main push that opens the pending-re-pin window, the
# ranges the re-pin WILL produce are asserted instead — an unpinned range converges
# on the sibling and admits it, while a deliberately pinned cap the re-pin preserves
# is still held to admitting the released sibling, so a real break is never masked.
_PENDING_REPIN_WINDOW = not (
    os.environ.get("GITHUB_HEAD_REF", "") and not os.environ["GITHUB_HEAD_REF"].startswith("release-please--")
)

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

# name -> version over every workspace member, normalized as range_sync keys it —
# the input its derivation and pin analysis read.
_FIRST_PARTY = range_sync.first_party_versions(range_sync.discover_members(ROOT))


def _effective_spec(
    dep_name: str, on_disk_spec: str, sibling_version: str, preserved: set[str], *, pending_repin: bool
) -> str:
    """The tai42-* range a member is held to for admission. On a development PR
    (``pending_repin`` False) it is the on-disk range, so a hand-authored range
    that refuses a sibling is caught. During the pending-re-pin window it is the
    range release-repin will produce: an unpinned dep converges on
    ``derive_range(sibling_version)`` (which admits that version), while a dep the
    re-pin preserves (a deliberate pinned cap) keeps its on-disk range, so a pin
    that refuses the released sibling still fails."""
    if not pending_repin or range_sync._normalize_name(dep_name) in preserved:
        return on_disk_spec
    return range_sync.derive_range(sibling_version)


def _preserved_first_party(pyproject: dict) -> set[str]:
    """Normalized names of this member's first-party deps whose cap range_sync
    preserves across a major (a deliberate ``[tool.range-sync]`` pin)."""
    analysis = range_sync.analyze_pyproject(pyproject, _FIRST_PARTY)
    return {range_sync._normalize_name(p.dep_name) for p in analysis.preserved}


def _assert_cap_admission(
    member_label: str, pyproject: dict, sibling_versions: dict[str, str], *, pending_repin: bool
) -> None:
    preserved = _preserved_first_party(pyproject)
    for dep_name, spec in _iter_tai42_specifiers(pyproject):
        if dep_name not in sibling_versions:
            continue  # not a workspace sibling
        sibling_version = sibling_versions[dep_name]
        effective = _effective_spec(dep_name, str(spec), sibling_version, preserved, pending_repin=pending_repin)
        assert SpecifierSet(effective).contains(sibling_version, prereleases=False), (
            f"{member_label}: {dep_name} {effective} does not admit sibling version {sibling_version}"
        )


@pytest.mark.parametrize("member_dir", PACKAGED_DIRS, ids=PACKAGED_PATHS)
def test_cap_admission(member_dir: Path):
    _assert_cap_admission(
        member_dir.name,
        _load_toml(member_dir / "pyproject.toml"),
        SIBLING_VERSIONS,
        pending_repin=_PENDING_REPIN_WINDOW,
    )


def test_cap_admission_window_admits_pending_repin_while_disk_refuses():
    """During the window a sibling major bump leaves a member's range stale; disk
    mode (a development PR) refuses it, window mode (main push / release train)
    admits the range the pending re-pin will produce."""
    dep, stale, bumped = "tai42-contract", ">=8.0,<9", "9.0.0"
    assert not SpecifierSet(_effective_spec(dep, stale, bumped, set(), pending_repin=False)).contains(bumped)
    assert SpecifierSet(_effective_spec(dep, stale, bumped, set(), pending_repin=True)).contains(bumped)


def test_cap_admission_window_still_refuses_a_preserved_pin_rejecting_sibling():
    """A deliberate pinned cap the re-pin preserves is held to admitting the
    released sibling even in window mode — a real break is never masked."""
    dep, pinned_cap, bumped = "tai42-contract", ">=7,<8", "9.0.0"
    preserved = {range_sync._normalize_name(dep)}
    assert not SpecifierSet(_effective_spec(dep, pinned_cap, bumped, preserved, pending_repin=True)).contains(bumped)


def test_cap_admission_helper_contrasts_modes_on_a_stale_member():
    """The assembled check: a member pinning a bumped sibling's stale range fails
    on a PR (disk) and passes in the window (the re-pin's range admits)."""
    pyproject = {"project": {"name": "tai42-demo", "version": "1.0.0", "dependencies": ["tai42-contract>=8.0,<9"]}}
    siblings = {"tai42-contract": "9.0.0"}
    with pytest.raises(AssertionError):
        _assert_cap_admission("demo", pyproject, siblings, pending_repin=False)
    _assert_cap_admission("demo", pyproject, siblings, pending_repin=True)


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
    admits the workspace contract version.

    A descriptor-only component carries no pyproject pin to preserve, so its
    ``contract:`` range follows the global derived range like an unpinned member:
    outside the pending-re-pin window the on-disk range is asserted (a hand-authored
    pin refusing the contract is caught on a development PR); inside it the range the
    re-pin will produce is asserted instead, so a contract major bump is not flagged
    against the not-yet-re-pinned descriptors on the train branch and its main push."""
    spec = load_plugin_spec(plugin_dir / "tai-plugin.yml")
    assert spec.package is None, f"{plugin_dir.name}: descriptor plugin must carry no package"

    validate_docs(read_dir_docs(plugin_dir), first_party=True)

    for sidecar in ("LICENSE", "icon.png", "CHANGELOG.md", "version.txt"):
        assert (plugin_dir / sidecar).is_file(), f"{plugin_dir.name}: missing {sidecar}"

    version_txt = (plugin_dir / "version.txt").read_text().strip()
    assert version_txt == spec.version, f"{plugin_dir.name}: version.txt={version_txt} != yml version {spec.version}"

    assert spec.contract is not None, f"{plugin_dir.name}: descriptor plugin must declare a contract range"
    effective = _effective_spec(
        range_sync.CONTRACT_PACKAGE, spec.contract, CONTRACT_VERSION, set(), pending_repin=_PENDING_REPIN_WINDOW
    )
    assert SpecifierSet(effective).contains(CONTRACT_VERSION, prereleases=True), (
        f"{plugin_dir.name}: contract {effective!r} does not admit workspace contract {CONTRACT_VERSION}"
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
