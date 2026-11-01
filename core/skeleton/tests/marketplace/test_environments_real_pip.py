"""Real-pip proofs of the plugin environments, offline, from a forged wheelhouse.

The unit tests assert pip's argv and fake its effect; these run the REAL pip the installer
shells, through :func:`run_pip`, against forged wheels (``--no-index --find-links``):

  * the plugin layer is one pip call of every in-process plugin, constrained to this
    image's installed versions: plugins whose requirements pip can resolve together land
    in the layer build beside the image, never in it;
  * two in-process plugins whose requirements conflict, or a plugin needing another
    version of a distribution this image provides, are refused as a dependency conflict
    naming pip's report, and no build is left;
  * an ``mcp-server`` package gets its own virtual environment, free of the image's
    constraint: it holds a version the image could not take, its console script runs from
    an empty environment, and it exposes that script alone, not its interpreter or a
    dependency's script.

Fixture distribution names are ``tai42-fixture-*`` so none collides with an image
distribution, except ``packaging``, forged at a version the image cannot take.
POSIX-only, matching the ecosystem's darwin/linux target (a venv's ``bin/python``).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import subprocess
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest
from packaging.utils import canonicalize_name

from tai42_skeleton.marketplace.environments.build import build_layer, build_server
from tai42_skeleton.marketplace.environments.console_scripts import package_console_scripts
from tai42_skeleton.marketplace.environments.layout import BuildInput, layer_dir, layer_site_dirs, read_build_record
from tai42_skeleton.marketplace.environments.targets import PluginPin, ServerCommand
from tai42_skeleton.marketplace.errors import PluginDependencyConflictError
from tai42_skeleton.marketplace.pip import PipRunner, run_pip


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return f"sha256={digest}"


def _make_wheel(
    wheelhouse: Path,
    name: str,
    version: str,
    *,
    requires: list[str] | None = None,
    package_files: dict[str, bytes] | None = None,
    console_scripts: dict[str, str] | None = None,
) -> None:
    """Forge a minimal, installable pure-python wheel into ``wheelhouse``.

    ``package_files`` maps paths under the ``name`` package to their bytes (default: one
    ``__init__.py``); ``console_scripts`` maps a script name to its ``module:function``.
    """
    dist_info = f"{name}-{version}.dist-info"
    metadata = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    metadata += [f"Requires-Dist: {req}" for req in requires or []]
    files: dict[str, bytes] = {
        f"{name}/{path}": data for path, data in (package_files or {"__init__.py": b"VALUE = 1\n"}).items()
    }
    files[f"{dist_info}/METADATA"] = ("\n".join(metadata) + "\n").encode()
    files[f"{dist_info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    if console_scripts:
        lines = ["[console_scripts]", *(f"{script} = {target}" for script, target in console_scripts.items())]
        files[f"{dist_info}/entry_points.txt"] = ("\n".join(lines) + "\n").encode()
    record = [f"{path},{_record_hash(data)},{len(data)}" for path, data in files.items()]
    record.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode()
    with zipfile.ZipFile(wheelhouse / f"{name}-{version}-py3-none-any.whl", "w") as zf:
        for path, data in files.items():
            zf.writestr(path, data)


def _offline(wheelhouse: Path) -> PipRunner:
    """:func:`run_pip`, resolving only from ``wheelhouse``."""

    async def runner(args: list[str], /, *, env: Mapping[str, str] | None = None) -> str:
        at = args.index("install") + 1
        return await run_pip([*args[:at], "--no-index", "--find-links", str(wheelhouse), *args[at:]], env=env)

    return runner


def _pin(name: str, version: str = "1.0", *, server_commands: tuple[ServerCommand, ...] = ()) -> PluginPin:
    item = BuildInput(ref=f"tai42/{name}", package=f"tai42-fixture-{name}", version=version, source="pypi", sha256=None)
    return PluginPin(input=item, artifact_ref=None, server_commands=server_commands)


def _version_at(sites: list[Path], name: str) -> str | None:
    """The version of ``name`` installed under ``sites``, or ``None``."""
    target = canonicalize_name(name)
    for dist in importlib.metadata.distributions(path=[str(site) for site in sites]):
        if dist.name and canonicalize_name(dist.name) == target:
            return dist.version
    return None


def _wheelhouse(tmp_path: Path) -> Path:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _make_wheel(wheelhouse, "tai42_fixture_dep", "1.0")
    _make_wheel(wheelhouse, "tai42_fixture_dep", "2.0")
    return wheelhouse


async def test_in_process_plugins_resolve_together_into_the_layer_build(tmp_path: Path) -> None:
    wheelhouse = _wheelhouse(tmp_path)
    _make_wheel(wheelhouse, "tai42_fixture_a", "1.0", requires=["tai42-fixture-dep>=1"])
    _make_wheel(wheelhouse, "tai42_fixture_b", "1.0", requires=["tai42-fixture-dep<2"])
    plugins_dir = tmp_path / "plugins"

    build = await build_layer(
        plugins_dir, [_pin("a"), _pin("b")], pip_runner=_offline(wheelhouse), subject="cannot install tai42/b 1.0"
    )

    sites = layer_site_dirs(build.path)
    assert (_version_at(sites, "tai42-fixture-a"), _version_at(sites, "tai42-fixture-b")) == ("1.0", "1.0")
    assert _version_at(sites, "tai42-fixture-dep") == "1.0"
    record = read_build_record(build.path)
    assert record is not None
    assert [item.label() for item in record.inputs] == ["tai42/a 1.0", "tai42/b 1.0"]


async def test_two_in_process_plugins_with_conflicting_requirements_are_a_dependency_conflict(tmp_path: Path) -> None:
    wheelhouse = _wheelhouse(tmp_path)
    _make_wheel(wheelhouse, "tai42_fixture_a", "1.0", requires=["tai42-fixture-dep<2"])
    _make_wheel(wheelhouse, "tai42_fixture_b", "1.0", requires=["tai42-fixture-dep>=2"])
    plugins_dir = tmp_path / "plugins"

    with pytest.raises(PluginDependencyConflictError) as caught:
        await build_layer(
            plugins_dir, [_pin("a"), _pin("b")], pip_runner=_offline(wheelhouse), subject="cannot install tai42/b 1.0"
        )

    text = str(caught.value)
    assert text.startswith(
        "cannot install tai42/b 1.0: its dependencies conflict with the installed plugins or this image — ERROR:"
    )
    assert "tai42-fixture-dep" in text
    assert list(layer_dir(plugins_dir).builds.iterdir()) == []


async def test_a_plugin_needing_another_version_of_an_image_distribution_is_refused(tmp_path: Path) -> None:
    image_packaging = importlib.metadata.version("packaging")
    wheelhouse = _wheelhouse(tmp_path)
    _make_wheel(wheelhouse, "packaging", "0.1")
    _make_wheel(wheelhouse, "tai42_fixture_a", "1.0", requires=["packaging<1"])
    plugins_dir = tmp_path / "plugins"

    with pytest.raises(PluginDependencyConflictError) as caught:
        await build_layer(
            plugins_dir, [_pin("a")], pip_runner=_offline(wheelhouse), subject="cannot install tai42/a 1.0"
        )

    assert "packaging" in str(caught.value)
    assert list(layer_dir(plugins_dir).builds.iterdir()) == []
    assert importlib.metadata.version("packaging") == image_packaging


_SERVER_MAIN = b"""\
import importlib.metadata
import sys


def main() -> None:
    print(importlib.metadata.version("packaging"), sys.prefix)
"""


async def test_a_server_package_runs_from_its_own_environment_with_a_version_the_image_cannot_take(
    tmp_path: Path,
) -> None:
    wheelhouse = _wheelhouse(tmp_path)
    _make_wheel(wheelhouse, "packaging", "0.1")
    _make_wheel(
        wheelhouse,
        "tai42_fixture_srv",
        "1.0",
        requires=["packaging<1", "tai42-fixture-tooldep"],
        package_files={"__init__.py": b"", "cli.py": _SERVER_MAIN},
        console_scripts={"tai42-fixture-srv-mcp": "tai42_fixture_srv.cli:main"},
    )
    _make_wheel(wheelhouse, "tai42_fixture_tooldep", "1.0", console_scripts={"dep-tool": "tai42_fixture_tooldep:main"})
    pin = _pin("srv", server_commands=(ServerCommand(item="srv", command="tai42-fixture-srv-mcp"),))

    build = await build_server(
        tmp_path / "plugins",
        pin,
        servers=(pin.input.package,),
        pip_runner=_offline(wheelhouse),
        subject=f"cannot install {pin.input.label()}",
    )

    # The venv's bin holds its interpreter and the dependency's script too; only the package's
    # own console script is exposed.
    assert (build.path / "bin" / "python").exists()
    assert (build.path / "bin" / "dep-tool").exists()
    script = build.path / "bin" / "tai42-fixture-srv-mcp"
    assert package_console_scripts(build.path, "tai42-fixture-srv") == {"tai42-fixture-srv-mcp": script}

    # Run as the stdio launch runs it, from an empty environment: the script names its
    # venv's interpreter, which imports the venv's own distributions.
    child = subprocess.run([str(script)], capture_output=True, text=True, env={}, check=False)
    assert child.returncode == 0, child.stdout + child.stderr
    assert child.stdout.split() == ["0.1", str(build.path)]
