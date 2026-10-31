"""The plugin prefix: settings, the sys.path activation (env-wins ordering + loud
failures), the stdio launch it declares, the write pre-flight, and the RECORD-based
prefix uninstall.

Every seam here is exercised against a real temp directory — a hand-built
``.dist-info`` + ``RECORD`` for the uninstall path, real ``sys.path`` mutation for
the activation path — so the file operations the feature relies on are asserted for
real, never mocked away.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp.client.stdio import get_default_environment
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_kit.transport import (
    ScriptLocation,
    StdioLaunch,
    declare_stdio_launch,
    declared_stdio_launch,
    get_mcp_transport,
)

from tai42_skeleton.marketplace import prefix as prefix_module
from tai42_skeleton.marketplace.errors import PluginPrefixError
from tai42_skeleton.marketplace.prefix import (
    PluginPrefixSettings,
    activate_prefix,
    configured_prefix,
    ensure_prefix_writable,
    environment_constraints,
    environment_distribution_version,
    plugin_script_locations,
    prefix_has_distribution,
    prefix_site_dirs,
    uninstall_from_prefix,
)


@pytest.fixture(autouse=True)
def _restore_stdio_launch() -> Iterator[None]:
    """Each activation's stdio launch declaration is undone after the test."""
    declared = declared_stdio_launch()
    yield
    declare_stdio_launch(declared)


# -- settings ----------------------------------------------------------------


def test_settings_reads_tai_plugins_prefix_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_PLUGINS_PREFIX", "/opt/plugins")
    assert PluginPrefixSettings().prefix == "/opt/plugins"


def test_settings_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_PLUGINS_PREFIX", raising=False)
    assert PluginPrefixSettings().prefix is None


def test_configured_prefix_reads_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefix_module, "plugin_prefix_settings", lambda: PluginPrefixSettings(prefix="/x"))
    assert configured_prefix() == "/x"


# -- site dirs ---------------------------------------------------------------


def test_prefix_site_dirs_are_under_the_prefix() -> None:
    dirs = prefix_site_dirs("/opt/plugins")
    assert dirs
    assert all(d.startswith("/opt/plugins") for d in dirs)
    assert all(d.endswith("site-packages") for d in dirs)


# -- activation: sys.path ordering + loud failures ---------------------------


def test_activate_prefix_noop_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefix_module, "configured_prefix", lambda: None)
    before = list(sys.path)
    activate_prefix()
    assert sys.path == before


def test_activate_prefix_appends_after_environment_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prefix = str(tmp_path / "pfx")
    monkeypatch.setattr(prefix_module, "configured_prefix", lambda: prefix)
    site = prefix_site_dirs(prefix)[0]
    env_site = sysconfig.get_paths()["purelib"]
    orig = list(sys.path)
    try:
        activate_prefix()
        # The prefix dir is on the path, created on disk, and sits at the END so the
        # environment shadows it for any package present in both.
        assert site in sys.path
        assert Path(site).is_dir()
        assert sys.path[-1] == site
        if env_site in sys.path:
            assert sys.path.index(site) > sys.path.index(env_site)
        # A second activation (a reload) adds no duplicate entry.
        activate_prefix()
        assert sys.path.count(site) == 1
    finally:
        sys.path[:] = orig


def test_activate_prefix_tolerates_an_existing_empty_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prefix = str(tmp_path / "pfx")
    Path(prefix_site_dirs(prefix)[0]).mkdir(parents=True)
    monkeypatch.setattr(prefix_module, "configured_prefix", lambda: prefix)
    orig = list(sys.path)
    try:
        activate_prefix()
        assert prefix_site_dirs(prefix)[0] in sys.path
    finally:
        sys.path[:] = orig


def test_activate_prefix_missing_and_uncreatable_raises_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A prefix under an ancestor that is a FILE cannot be created — a set-but-broken
    # prefix fails loudly at boot rather than silently activating an empty path.
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x", encoding="utf-8")
    prefix = str(a_file / "sub")
    monkeypatch.setattr(prefix_module, "configured_prefix", lambda: prefix)
    orig = list(sys.path)
    try:
        with pytest.raises(OSError, match=r"Not a directory"):
            activate_prefix()
    finally:
        sys.path[:] = orig


# -- activation: the stdio launch -------------------------------------------


def _write_console_script(directory: str, name: str) -> Path:
    """Write an executable stand-in for a console script into ``directory``."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    script = Path(directory) / name
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _stdio_server(command: str) -> dict[str, object]:
    """The dict config the stdio MCP transport for a bare ``command`` server launches."""
    config = TaiMCPConfig(title="srv", config=MCPConfig(command=command, args=["--stdio"]))
    transport = get_mcp_transport(config)
    assert isinstance(transport, dict)
    return transport["mcpServers"]["srv"]


def _stdio_command(command: str) -> str:
    """The command the stdio MCP transport for a bare ``command`` server launches."""
    resolved = _stdio_server(command)["command"]
    assert isinstance(resolved, str)
    return resolved


def test_activate_prefix_declares_the_prefix_scripts_and_site_dirs_as_the_stdio_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prefix = str(tmp_path / "pfx")
    monkeypatch.setattr(prefix_module, "configured_prefix", lambda: prefix)
    prefix_scripts = sysconfig.get_paths(vars={"base": prefix, "platbase": prefix})["scripts"]
    orig = list(sys.path)
    try:
        activate_prefix()
        assert declared_stdio_launch() == StdioLaunch(
            (
                ScriptLocation(sysconfig.get_path("scripts")),
                ScriptLocation(prefix_scripts, tuple(prefix_site_dirs(prefix))),
            )
        )
        assert list(declared_stdio_launch().locations) == plugin_script_locations(prefix)
    finally:
        sys.path[:] = orig


def test_without_a_prefix_activation_declares_the_environment_scripts_dir_and_no_import_dirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prefix_module, "configured_prefix", lambda: None)
    activate_prefix()
    assert declared_stdio_launch() == StdioLaunch((ScriptLocation(sysconfig.get_path("scripts")),))


def test_an_activated_prefix_launches_the_console_script_installed_in_its_bin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A marketplace install into the prefix puts the plugin's console script in the
    # prefix's scripts dir, which is not on PATH: the stdio launch finds it there.
    prefix = str(tmp_path / "pfx")
    monkeypatch.setattr(prefix_module, "configured_prefix", lambda: prefix)
    script = _write_console_script(sysconfig.get_paths(vars={"base": prefix, "platbase": prefix})["scripts"], "pfx-srv")
    orig = list(sys.path)
    try:
        activate_prefix()
        assert _stdio_command("pfx-srv") == str(script)
    finally:
        sys.path[:] = orig


def test_without_a_prefix_the_stdio_launch_searches_the_interpreter_scripts_dir_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no prefix, installs land in the running interpreter's environment: its
    # scripts dir is searched before PATH, even when PATH does not carry it.
    monkeypatch.setattr(prefix_module, "configured_prefix", lambda: None)
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    scripts = sysconfig.get_path("scripts")
    activate_prefix()
    assert _stdio_command(Path(sys.executable).name) == str(Path(scripts) / Path(sys.executable).name)


def test_an_activated_prefix_launches_its_console_script_with_the_prefix_importable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The layout ``pip install --prefix`` writes: the plugin package under the prefix's
    # site dir and its console script in the prefix's scripts dir, with this interpreter
    # as its shebang. The launched child imports the package from the prefix.
    prefix = str(tmp_path / "pfx")
    monkeypatch.setattr(prefix_module, "configured_prefix", lambda: prefix)
    package = Path(prefix_site_dirs(prefix)[0]) / "prefix_launch_probe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("print(__file__)\n", encoding="utf-8")
    scripts = Path(sysconfig.get_paths(vars={"base": prefix, "platbase": prefix})["scripts"])
    scripts.mkdir(parents=True)
    script = scripts / "prefix-launch-probe"
    script.write_text(f"#!{sys.executable}\nimport prefix_launch_probe\n", encoding="utf-8")
    script.chmod(0o755)
    orig = list(sys.path)
    try:
        activate_prefix()
        server = _stdio_server("prefix-launch-probe")
    finally:
        sys.path[:] = orig
    command, env = server["command"], server["env"]
    assert isinstance(command, str)
    assert isinstance(env, dict)

    launched = subprocess.run(
        [command], env={**get_default_environment(), **env}, capture_output=True, text=True, check=False
    )

    assert launched.returncode == 0, launched.stderr
    assert str(package) in launched.stdout


def test_an_entry_path_names_the_executable_the_child_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A bare ``python`` on the entry's own PATH is that PATH's interpreter, not the
    # environment's scripts dir copy.
    monkeypatch.setattr(prefix_module, "configured_prefix", lambda: None)
    other = _write_console_script(str(tmp_path / "other" / "bin"), Path(sys.executable).name)
    activate_prefix()
    config = TaiMCPConfig(
        title="srv", config=MCPConfig(command=other.name, args=["server.py"], env={"PATH": str(other.parent)})
    )
    transport = get_mcp_transport(config)
    assert isinstance(transport, dict)
    assert transport["mcpServers"]["srv"]["command"] == str(other)


def test_a_script_in_the_environment_scripts_dir_launches_without_the_prefix_on_its_pythonpath(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prefix = str(tmp_path / "pfx")
    monkeypatch.setattr(prefix_module, "configured_prefix", lambda: prefix)
    orig = list(sys.path)
    try:
        activate_prefix()
        server = _stdio_server(Path(sys.executable).name)
    finally:
        sys.path[:] = orig
    env = server["env"]
    assert isinstance(env, dict)
    assert server["command"] == str(Path(sysconfig.get_path("scripts")) / Path(sys.executable).name)
    assert "PYTHONPATH" not in env


# -- install write pre-flight ------------------------------------------------


def test_ensure_prefix_writable_creates_site_dirs(tmp_path: Path) -> None:
    prefix = str(tmp_path / "pfx")
    ensure_prefix_writable(prefix)
    assert Path(prefix_site_dirs(prefix)[0]).is_dir()
    # The write probe left nothing behind.
    assert not (Path(prefix) / ".tai42-prefix-write-probe").exists()


def test_ensure_prefix_writable_raises_on_unwritable(tmp_path: Path) -> None:
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x", encoding="utf-8")
    prefix = str(a_file / "sub")
    with pytest.raises(PluginPrefixError, match="not writable"):
        ensure_prefix_writable(prefix)


# -- prefix uninstall: RECORD-based removal ----------------------------------


def _install_fake_dist(prefix: str, package: str, version: str, *, record_paths: list[str] | None = None) -> Path:
    """Write a minimal installed distribution (a package dir + ``.dist-info`` with
    ``METADATA`` and ``RECORD``) into the prefix's site directory, the way a
    ``pip install --prefix`` would lay one out, and return that site directory."""
    site = Path(prefix_site_dirs(prefix)[0])
    site.mkdir(parents=True, exist_ok=True)
    pkg_dir = site / package
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    info = site / f"{package}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n", encoding="utf-8")
    lines = record_paths or [
        f"{package}/__init__.py,,",
        f"{package}-{version}.dist-info/METADATA,,",
        f"{package}-{version}.dist-info/RECORD,,",
    ]
    (info / "RECORD").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return site


# -- env vs prefix distribution resolution -----------------------------------


def test_prefix_has_distribution_true_for_prefix_dist_and_false_for_absent(tmp_path: Path) -> None:
    prefix = str(tmp_path / "pfx")
    _install_fake_dist(prefix, "foo", "1.0")
    assert prefix_has_distribution("foo", prefix) is True
    assert prefix_has_distribution("bar", prefix) is False


def test_prefix_has_distribution_ignores_environment_only_copy(tmp_path: Path) -> None:
    # A distribution present only in the ENVIRONMENT (never written to the prefix) is
    # not reported as a prefix distribution — the check scans the prefix site dirs only.
    prefix = str(tmp_path / "pfx")
    Path(prefix_site_dirs(prefix)[0]).mkdir(parents=True)
    assert prefix_has_distribution("pytest", prefix) is False


def test_environment_distribution_version_reads_env_not_prefix(tmp_path: Path) -> None:
    # A real environment package reports its installed version; a package that lives
    # ONLY under the prefix is invisible to the environment read.
    prefix = str(tmp_path / "pfx")
    _install_fake_dist(prefix, "foo", "9.9")
    assert environment_distribution_version("pytest", prefix) is not None
    assert environment_distribution_version("foo", prefix) is None


# -- environment_constraints: the prefix-install constraint freeze -----------


def _write_env_dist(site: Path, name: str, version: str | None) -> None:
    """Write a minimal installed distribution (just a ``.dist-info`` with METADATA)
    into ``site``. A ``None`` version writes METADATA with no ``Version`` line, so
    ``importlib.metadata`` reports the distribution's version as ``None``."""
    site.mkdir(parents=True, exist_ok=True)
    info = site / f"{name}-{version or '0'}.dist-info"
    info.mkdir()
    lines = ["Metadata-Version: 2.1", f"Name: {name}"]
    if version is not None:
        lines.append(f"Version: {version}")
    (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_nameless_dist(site: Path, dirname: str) -> None:
    """Write a ``.dist-info`` whose METADATA carries no ``Name`` field, so
    ``importlib.metadata`` reports its name as ``None``."""
    site.mkdir(parents=True, exist_ok=True)
    info = site / f"{dirname}-0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nVersion: 9.9\n", encoding="utf-8")


def test_environment_constraints_excludes_prefix_first_wins_sorted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Two environment dirs plus the prefix sit on sys.path. The freeze names every
    # ENVIRONMENT distribution as ``name==version`` — sorted, one per name with the
    # first sys.path occurrence winning — and NEVER a distribution living under the
    # prefix (that is what the prefix install is about to add).
    prefix = str(tmp_path / "pfx")
    env_a = tmp_path / "env_a"
    env_b = tmp_path / "env_b"
    _write_env_dist(env_a, "widget", "1.1")
    _write_env_dist(env_a, "alpha", "0.3")
    _write_env_dist(env_b, "widget", "9.9")  # masked: env_a precedes env_b on sys.path
    _write_env_dist(Path(prefix_site_dirs(prefix)[0]), "pluginpkg", "5.0")  # under the prefix -> excluded
    monkeypatch.setattr(prefix_module.sys, "path", [str(env_a), str(env_b), *prefix_site_dirs(prefix)])

    assert environment_constraints(prefix) == ["alpha==0.3", "widget==1.1"]


def test_environment_constraints_skips_a_nameless_distribution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A distribution whose metadata carries no Name cannot be named in a constraint,
    # so it is passed over rather than raising — only the named one is emitted.
    env = tmp_path / "env"
    _write_env_dist(env, "widget", "1.0")
    _write_nameless_dist(env, "mystery")
    monkeypatch.setattr(prefix_module.sys, "path", [str(env)])

    assert environment_constraints(str(tmp_path / "pfx")) == ["widget==1.0"]


def test_environment_constraints_raises_on_versionless_named_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A NAMED distribution with no version is a loud failure: omitting it silently
    # would let the resolver replace it with another version in the prefix.
    env = tmp_path / "env"
    _write_env_dist(env, "widget", None)
    monkeypatch.setattr(prefix_module.sys, "path", [str(env)])

    with pytest.raises(PluginPrefixError, match="no version"):
        environment_constraints(str(tmp_path / "pfx"))


def test_uninstall_from_prefix_removes_named_dist_and_keeps_dependencies(tmp_path: Path) -> None:
    prefix = str(tmp_path / "pfx")
    site = _install_fake_dist(prefix, "foo", "1.0")
    _install_fake_dist(prefix, "bar", "2.0")  # a dependency foo pulled in

    uninstall_from_prefix("foo", prefix)

    # The named distribution's own files are gone (package tree + dist-info).
    assert not (site / "foo").exists()
    assert not (site / "foo-1.0.dist-info").exists()
    # The dependency stays — only the named distribution is removed, exactly like
    # ``pip uninstall`` on the environment path.
    assert (site / "bar" / "__init__.py").exists()
    assert (site / "bar-2.0.dist-info").exists()


def test_uninstall_from_prefix_absent_package_raises_and_touches_nothing(tmp_path: Path) -> None:
    prefix = str(tmp_path / "pfx")
    Path(prefix_site_dirs(prefix)[0]).mkdir(parents=True)
    with pytest.raises(PluginPrefixError, match="not installed in the plugin prefix"):
        uninstall_from_prefix("foo", prefix)


def test_uninstall_from_prefix_record_escape_raises_and_deletes_nothing_outside(tmp_path: Path) -> None:
    prefix = str(tmp_path / "pfx")
    outside = tmp_path / "evil.txt"
    outside.write_text("keep me", encoding="utf-8")
    site = Path(prefix_site_dirs(prefix)[0])
    # Enough ``..`` segments to climb out of the prefix entirely.
    depth = len(site.relative_to(tmp_path).parts)
    escape = "/".join([".."] * depth) + "/evil.txt"
    _install_fake_dist(prefix, "foo", "1.0", record_paths=[escape + ",,", "foo-1.0.dist-info/RECORD,,"])

    with pytest.raises(PluginPrefixError, match="escapes the plugin prefix"):
        uninstall_from_prefix("foo", prefix)
    # A RECORD path outside the prefix is refused before deletion — the outside file
    # is untouched.
    assert outside.read_text(encoding="utf-8") == "keep me"


# -- a distribution whose metadata carries no Name is never a match ----------


def test_prefix_has_distribution_passes_over_a_nameless_distribution(tmp_path: Path) -> None:
    # A nameless .dist-info cannot be the target, even one whose directory carries the
    # target's name: only the named distribution answers.
    prefix = str(tmp_path / "pfx")
    site = _install_fake_dist(prefix, "foo", "1.0")
    _write_nameless_dist(site, "bar")

    assert prefix_has_distribution("foo", prefix) is True
    assert prefix_has_distribution("bar", prefix) is False


def test_environment_distribution_version_passes_over_a_nameless_distribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The nameless .dist-info sits earlier on sys.path, so the scan reaches it first.
    stray = tmp_path / "stray"
    env = tmp_path / "env"
    _write_nameless_dist(stray, "widget")
    _write_env_dist(env, "widget", "1.0")
    monkeypatch.setattr(prefix_module.sys, "path", [str(stray), str(env)])

    assert environment_distribution_version("widget", str(tmp_path / "pfx")) == "1.0"


def test_uninstall_from_prefix_passes_over_a_nameless_distribution(tmp_path: Path) -> None:
    prefix = str(tmp_path / "pfx")
    site = _install_fake_dist(prefix, "foo", "1.0")
    _write_nameless_dist(site, "foo")

    uninstall_from_prefix("foo", prefix)

    assert not (site / "foo-1.0.dist-info").exists()
    assert (site / "foo-0.dist-info").exists()
