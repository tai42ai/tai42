"""A plugin whose own dependency pin rejects the image, removed from the host with no server.
Opt-in: collects only with ``TAI_E2E_MARKETPLACE=1``.

Zeta is installed online, so its install-store row, its plugin layer build and its manifest
entry all exist. The image then moves under it: the layer's current build holds a zeta whose
declared contract range still covers the running contract but whose ``Requires-Dist`` pins
``tai42-contract`` to a line the image does not run, and the store row names that version. A
fresh serve process refuses to start, naming the unmet requirement; the real ``tai plugins
rebuild`` stops with pip's conflict, because the plugin's range keeps it in the layer and its
pin cannot resolve against the image. The running-server uninstall cannot be reached.

``tai plugins uninstall --offline`` is the way out: run on the host with the deployment's env,
it removes the row, rebuilds the layer without zeta and strips zeta from the manifest, and the
same serve process then starts and serves.

The forged state is the one an image upgrade leaves: no API can create it, because install and
update resolve a plugin's pins against the image. The conflicting wheel is laid with
``--no-deps`` and served by the fixture package index, so the rebuild's resolution, not a
missing file, is what refuses.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from tai42_skeleton.marketplace.environments.layout import BuildInput

from tai42_e2e.catalog_seed import forge_zeta_wheel
from tai42_e2e.fixture_catalog import (
    ZETA_COMPAT_VERSION,
    ZETA_NARROW_CONTRACT_RANGE,
    ZETA_PACKAGE,
    ZETA_REF,
    ZETA_TOOLS_MODULE,
    ZETA_WIDE_CONTRACT_RANGE,
)
from tai42_e2e.pkgsource import BuiltWheel, FixturePackageIndex
from tai42_e2e.plugin_layer import lay_wheel_as_current_layer, move_install_row_version
from tai42_e2e.stack import TaiStack

from ._market_support import (
    cli_env,
    host_command_env,
    installed_refs,
    layer_holds,
    ok_json,
    persisted_manifest,
    run_cli,
    run_rebuild,
    stack_plugins_dir,
    tai_bin,
    wait_tool_absent,
)

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "kind:marketplace_store",
        "mutable",
        "files",
        "process",
        "cli",
        "helper:registry",
        "helper:package-index",
        "setting:MARKETPLACE_URL",
        "setting:PIP_INDEX_URL",
        "setting:TAI_PLUGINS_DIR",
        "setting:fixture-plugin:zeta",
    ),
]

_ZETA_TOOL = "e2e_zeta_probe"
# The version the image upgrade leaves installed: newer than the published compatible one.
_PINNED_VERSION = "0.1.1"
# The note the offline uninstall ends its receipt with.
_NEXT_START = "a server loads the uninstall on its next start"


def _run_offline_uninstall(stack: TaiStack, home: Path, ref: str) -> subprocess.CompletedProcess[str]:
    """Run the real ``tai --json plugins uninstall --offline <ref>`` with the deployment's env, as
    an operator on the host does: no server URL and no API key."""
    return subprocess.run(
        [tai_bin(), "--json", "plugins", "uninstall", "--offline", ref],
        env=host_command_env(stack, home),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _forge_the_image_moving_under_zeta(
    stack: TaiStack, package_index: FixturePackageIndex, tmp_path: Path
) -> BuiltWheel:
    """Make zeta's current layer build and its store row the ones an image upgrade leaves."""
    out_dir = tmp_path / "pinned"
    out_dir.mkdir()
    pinned = forge_zeta_wheel(
        _PINNED_VERSION,
        out_dir,
        contract_range=ZETA_WIDE_CONTRACT_RANGE,
        requires_dist_range=ZETA_NARROW_CONTRACT_RANGE,
    )
    package_index.register(pinned)
    built_from = BuildInput(ref=ZETA_REF, package=ZETA_PACKAGE, version=_PINNED_VERSION, source="pypi", sha256=None)
    lay_wheel_as_current_layer(stack_plugins_dir(stack), pinned.path, built_from)
    move_install_row_version(stack.resources, ZETA_REF, _PINNED_VERSION)
    return pinned


def _manifest_names_zeta(stack: TaiStack) -> bool:
    return ZETA_TOOLS_MODULE in json.dumps(persisted_manifest(stack))


async def test_an_offline_uninstall_removes_a_plugin_whose_pin_rejects_the_image_and_the_server_starts(
    marketplace_stack: TaiStack,
    package_index: FixturePackageIndex,
    zeta_catalog: tuple[BuiltWheel, BuiltWheel],
    tmp_path: Path,
) -> None:
    stack = marketplace_stack
    installed = ok_json(run_cli(cli_env(stack, tmp_path), "install", ZETA_REF))
    assert installed["version"] == ZETA_COMPAT_VERSION, installed
    assert ZETA_REF in await installed_refs(stack)
    assert layer_holds(stack, ZETA_PACKAGE)
    assert _manifest_names_zeta(stack)

    stack.kill("serve")
    _forge_the_image_moving_under_zeta(stack, package_index, tmp_path)

    # The dead end: the server refuses to start and the rebuild cannot resolve zeta's pin.
    with pytest.raises(RuntimeError) as refused:
        stack.restart("serve")
    refusal = str(refused.value)
    assert "plugin environment is inconsistent with this image — refusing to start" in refusal, refusal
    assert f"{ZETA_PACKAGE} {_PINNED_VERSION} requires tai42-contract" in refusal, refusal
    rebuilt = run_rebuild(stack, tmp_path)
    assert rebuilt.returncode != 0, f"rebuild succeeded: {rebuilt.stdout}\n{rebuilt.stderr}"
    assert "its dependencies conflict with the installed plugins or this image" in rebuilt.stderr, rebuilt.stderr

    # The way out, on the host with no server.
    uninstalled = _run_offline_uninstall(stack, tmp_path, ZETA_REF)
    assert uninstalled.returncode == 0, f"{uninstalled.stdout}\n{uninstalled.stderr}"
    receipt = json.loads(uninstalled.stdout)
    assert receipt["ref"] == ZETA_REF
    assert receipt["uninstalled"] is True
    assert receipt["manifest_written"] is True
    assert receipt["notes"][-1] == _NEXT_START
    assert not layer_holds(stack, ZETA_PACKAGE)
    assert not _manifest_names_zeta(stack)

    # The same serve process starts and serves, with zeta gone.
    stack.restart("serve")
    assert ZETA_REF not in await installed_refs(stack)
    await wait_tool_absent(stack, _ZETA_TOOL)
    async with stack.mcp() as mcp:
        echoed = await mcp.call_tool("e2e_echo", {"payload": "after-offline-uninstall"})
    assert not echoed.is_error, echoed


def test_an_offline_uninstall_of_a_plugin_that_is_not_installed_exits_non_zero_naming_it(
    marketplace_stack: TaiStack, tmp_path: Path
) -> None:
    ref = "tai42/e2e-not-installed"

    proc = _run_offline_uninstall(marketplace_stack, tmp_path, ref)

    assert proc.returncode == 1, f"{proc.stdout}\n{proc.stderr}"
    assert f"{ref} is not installed" in proc.stderr, proc.stderr
