"""The boot check of the platform features a loaded plugin requires, driven through live stacks.

A real wheel, built from the ``fixtures/requiring_plugin`` source tree, is installed into
the plugins prefix of every stack here. Its packaged spec declares ``requires:
[versioning]``, a feature that is on exactly when the stack's default database is
configured. The stacks are the bare profile with ``TAI_PLUGINS_PREFIX`` pointed at that
prefix, with or without the default database:

- without the database, a manifest that loads the plugin's tools module refuses to start:
  the serve process exits naming the plugin, the feature and the env var that turns it on;
- with the database, the same manifest boots and the plugin's tool answers;
- on a booted database-less stack, replacing the manifest with one that loads the plugin
  is refused with a 500 carrying the same refusal, and the running epoch keeps serving.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from tai42_e2e import StackConfig, StackResources
from tai42_e2e.manifests import build_bare_stack
from tai42_e2e.pkgsource import build_fixture_wheel
from tai42_e2e.stack import TaiStack
from tai42_e2e.variants import Variants

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs("no-stack", "process", "files", "setting:TAI_PLUGINS_PREFIX"),
]

_FIXTURE_SOURCE = Path(__file__).resolve().parents[2] / "fixtures" / "requiring_plugin"
_PLUGIN_REF = "tai42/e2e-requiresprobe"
_TOOLS_MODULE = "tai_e2e_requiresprobe.tools"
_TOOL = "e2e_requiresprobe_ping"
_ENABLING_VAR = "TAI_DATABASE_DEFAULT_PG_PASSWORD"
_DATABASE_ENV_PREFIX = "TAI_DATABASE_DEFAULT_"


@pytest.fixture(scope="module")
def plugins_prefix(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A plugins prefix holding the requiring plugin's distribution, installed from its built wheel.

    ``--no-deps`` because only the plugin's own distribution lands in a prefix; its one
    dependency, ``tai42-contract``, is the environment's own.
    """
    wheel = build_fixture_wheel(_FIXTURE_SOURCE, "1.0.0", tmp_path_factory.mktemp("requiring-wheel"))
    prefix = tmp_path_factory.mktemp("requiring-prefix")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--prefix", str(prefix), str(wheel.path)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pip install --prefix failed for {wheel.path.name} (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
    return prefix


def _loading_the_plugin(manifest: dict[str, object]) -> dict[str, object]:
    """``manifest`` with the plugin's tools module appended as the installer writes it."""
    tools = manifest.get("tools", [])
    assert isinstance(tools, list)
    return {**manifest, "tools": [*tools, {"title": _TOOLS_MODULE, "module": _TOOLS_MODULE}]}


def _prefix_stack(
    prefix: Path, *, database: bool, load_plugin: bool
) -> Callable[[StackResources, Variants], StackConfig]:
    """A bare-stack builder with ``prefix`` as the plugins prefix, the default database on or off,
    and the plugin's tools module in the manifest or not."""

    def builder(res: StackResources, variants: Variants) -> StackConfig:
        base = build_bare_stack(res, variants)
        env = {**base.env, "TAI_PLUGINS_PREFIX": str(prefix)}
        if not database:
            env = {key: value for key, value in env.items() if not key.startswith(_DATABASE_ENV_PREFIX)}
        manifest = _loading_the_plugin(base.manifest) if load_plugin else base.manifest
        return replace(base, name="plugin-requires-gate", env=env, manifest=manifest)

    return builder


def _assert_names_the_refusal(text: str) -> None:
    assert f"plugin {_PLUGIN_REF!r} requires the Versioning feature ('versioning')" in text, text
    assert f"which is off: set {_ENABLING_VAR}" in text, text


async def _tool_names(stack: TaiStack) -> set[str]:
    async with stack.mcp() as mcp:
        return {tool.name for tool in await mcp.list_tools()}


async def test_boot_refuses_a_plugin_requiring_an_off_feature(
    fresh_stack: Callable[..., TaiStack], plugins_prefix: Path
) -> None:
    # The serve process exits before it serves, and the readiness wait surfaces its
    # refusal from the log tail.
    with pytest.raises(RuntimeError) as refused:
        fresh_stack(_prefix_stack(plugins_prefix, database=False, load_plugin=True))
    _assert_names_the_refusal(str(refused.value))


async def test_boot_serves_a_plugin_whose_required_feature_is_on(
    fresh_stack: Callable[..., TaiStack], plugins_prefix: Path
) -> None:
    stack = fresh_stack(_prefix_stack(plugins_prefix, database=True, load_plugin=True))
    async with stack.mcp() as mcp:
        result = await mcp.call_tool(_TOOL, {})
    assert result.data == "requiresprobe-pong", result


@pytest.mark.needs("mutable")
async def test_a_reload_loading_a_plugin_requiring_an_off_feature_is_refused(
    fresh_stack: Callable[..., TaiStack], plugins_prefix: Path
) -> None:
    stack = fresh_stack(_prefix_stack(plugins_prefix, database=False, load_plugin=False))
    before = await _tool_names(stack)
    assert before, "the database-less stack serves no tools"
    assert _TOOL not in before

    manifest_path = stack.root / "config" / "manifest.yml"
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    resp = await stack.api().request_raw(
        "POST",
        "/api/manifest/replace",
        json={"manifest_text": yaml.safe_dump(_loading_the_plugin(document))},
        timeout=60.0,
    )

    # The reload's epoch build refuses before it imports the plugin, and the running
    # epoch keeps serving the tools it served.
    assert resp.status_code == 500, f"{resp.status_code} {resp.text}"
    _assert_names_the_refusal(resp.text)
    assert await _tool_names(stack) == before
