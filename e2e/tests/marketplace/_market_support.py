"""Shared drivers for the marketplace specs.

The install → live-tool → uninstall arc is the area's core promise and is
exercised by several specs (install lifecycle, update, advisories, CLI parity),
so its waits and its clean-tail assertions live here once. Every wait is a
:func:`~tai42_e2e.waiting.wait_for_async` over a real observable — a tool answering
over MCP, a row in the attribution store, a distribution in a plugin environment —
never a sleep.

The plugin environment readers (what the stack's plugin layer and a server
package's environment hold, read from disk under ``TAI_PLUGINS_DIR``, and what the
image — the harness interpreter's own environment — holds) live here too, shared by
every spec that proves where an install landed.

The eta fixture's MCP-server drivers (its go-live wait over ``/api/mcp-status`` and
``/mcp``) live here too, shared by the mcp-server install and update specs.

The ``tai plugins`` CLI drivers (the real venv ``tai`` binary against a stack, and
``tai plugins rebuild`` run with a stack's own deployment env)
and the plugin-compat response readers (the resolve path with a ``contract``
pin, the per-ref upgrade-all outcomes, an installed row's compat block) live
here too, shared by the CLI-parity and compat specs.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from fastmcp.client.client import CallToolResult
from packaging.utils import canonicalize_name

from tai42_e2e.child_env import child_env
from tai42_e2e.fixture_catalog import ETA_MCP_TITLE, ETA_MCP_TOOL
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async


def probe_payload(result: CallToolResult) -> dict[str, Any]:
    """The dict a fixture probe tool returned, from a fastmcp call result.

    A structured-output tool surfaces its value on ``data`` (a str-returning tool
    would surface a str; the fixtures return dicts); ``structured_content`` is the
    fallback when ``data`` is unset."""
    payload: Any = result.data if result.data is not None else result.structured_content
    if not isinstance(payload, dict):
        raise AssertionError(f"probe tool did not return an object result: {payload!r}")
    return payload


async def api_tool_names(stack: TaiStack) -> list[str]:
    """The server's registered tool names over ``GET /api/tools``."""
    names = await stack.api().get("/api/tools")
    if not isinstance(names, list):
        raise AssertionError(f"/api/tools did not return a list: {names!r}")
    return names


async def wait_tool_live(stack: TaiStack, tool_name: str, *, dist_version: str, deadline: float = 30.0) -> None:
    """Wait until ``tool_name`` is callable over MCP and answers ``dist_version``.

    A fresh MCP session per poll so a just-reloaded tool registration is observed
    (a cached session would keep the pre-install tool list). The same tool must
    also appear in ``GET /api/tools``."""

    async def live() -> bool:
        async with stack.mcp() as mcp:
            if tool_name not in await mcp.tool_names():
                return False
            result = await mcp.call_tool(tool_name, {}, raise_on_error=False)
        if result.is_error:
            return False
        if probe_payload(result).get("dist_version") != dist_version:
            return False
        return tool_name in await api_tool_names(stack)

    await wait_for_async(
        live,
        deadline=deadline,
        message=f"tool {tool_name!r} never became live at dist_version {dist_version!r}",
    )


async def wait_tool_absent(stack: TaiStack, tool_name: str, *, deadline: float = 30.0) -> None:
    """Wait until ``tool_name`` is neither in ``GET /api/tools`` nor callable over MCP."""

    async def absent() -> bool:
        if tool_name in await api_tool_names(stack):
            return False
        async with stack.mcp() as mcp:
            return tool_name not in await mcp.tool_names()

    await wait_for_async(absent, deadline=deadline, message=f"tool {tool_name!r} never disappeared after uninstall")


class MarketInstaller:
    """Abort-safe install ledger for one test: every install it performs is uninstalled in
    the fixture's teardown even if the test body raises before its own clean-tail. It is
    belt-and-suspenders, NOT the tripwire — every spec's own clean-tail asserts the plugin
    is gone, and the session ``_image_state_guard`` asserts no install ever reached the image."""

    def __init__(self) -> None:
        self._installed: list[tuple[TaiStack, str, str]] = []

    async def install(
        self,
        stack: TaiStack,
        ref: str,
        package: str,
        *,
        version: str | None = None,
        accept_public_routes: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Install ``ref`` on ``stack`` with the install-time ``env``, ledgered for teardown, and return
        the install receipt."""
        body: dict[str, Any] = {"ref": ref}
        if version is not None:
            body["version"] = version
        if env is not None:
            body["env"] = dict(env)
        if accept_public_routes:
            # A fixture that declares a PUBLIC route (epsilon's GET /api/e2e-epsilon/open)
            # requires explicit acceptance, mirroring the field the server + the route
            # spec's own install helper use, or the routes-capable registry returns 400
            # PUBLIC_ROUTES_NOT_ACCEPTED.
            body["accept_public_routes"] = True
        receipt = await stack.api().post("/api/marketplace/install", json=body, timeout=timeout)
        self._installed.append((stack, ref, package))
        return receipt

    async def acleanup(self) -> None:
        # Reverse order, best-effort: a teardown must never mask the body's own exception,
        # and a ref the test already uninstalled (its clean-tail ran) is skipped.
        for stack, ref, _package in reversed(self._installed):
            with contextlib.suppress(Exception):
                if ref in await installed_refs(stack):
                    await stack.api().post("/api/marketplace/uninstall", json={"ref": ref})


def stack_plugins_dir(stack: TaiStack) -> Path:
    """The stack's plugins directory (``TAI_PLUGINS_DIR``)."""
    return Path(stack.config.env["TAI_PLUGINS_DIR"])


def layer_site_packages(stack: TaiStack) -> list[Path]:
    """The site-packages directories of the stack's current plugin layer build."""
    return sorted((stack_plugins_dir(stack) / "layer" / "current").glob("lib/python*/site-packages"))


def server_environment(stack: TaiStack, package: str) -> Path:
    """The current build of ``package``'s own server environment (a virtual environment)."""
    return stack_plugins_dir(stack) / "servers" / canonicalize_name(package) / "current"


# The start and the end of the exec line pip writes around the interpreter in a trampoline.
_TRAMPOLINE_EXEC = "'''exec' "
_TRAMPOLINE_ARGS = ' "$0" "$@"'


def console_script_interpreter(script: Path) -> Path:
    """The interpreter a console script runs: the one its shebang names, or, when that
    path is too long for a shebang (127 bytes on Linux) or holds a space, the one the
    ``/bin/sh`` trampoline pip writes in its place execs on its second line
    (``'''exec' <interpreter> "$0" "$@"``, the interpreter double-quoted when it holds
    a space)."""
    first, second = script.read_text(encoding="utf-8").splitlines()[:2]
    shebang = first.removeprefix("#!").strip()
    if shebang != "/bin/sh":
        return Path(shebang)
    if not (second.startswith(_TRAMPOLINE_EXEC) and second.endswith(_TRAMPOLINE_ARGS)):
        raise AssertionError(f"{script} runs /bin/sh with no exec trampoline on its second line: {second!r}")
    words = shlex.split(second[len(_TRAMPOLINE_EXEC) : -len(_TRAMPOLINE_ARGS)])
    if len(words) != 1:
        raise AssertionError(f"{script}: its exec trampoline names {words!r}, not one interpreter")
    return Path(words[0])


def _holds(paths: list[Path] | None, package: str) -> bool:
    importlib.invalidate_caches()
    wanted = canonicalize_name(package)
    found = (
        importlib.metadata.distributions()
        if paths is None
        else importlib.metadata.distributions(path=[str(p) for p in paths])
    )
    return any(canonicalize_name(dist.metadata["Name"]) == wanted for dist in found)


def layer_holds(stack: TaiStack, package: str) -> bool:
    """Whether the stack's current plugin layer build holds ``package``, read from disk."""
    return _holds(layer_site_packages(stack), package)


def server_environment_holds(stack: TaiStack, package: str, distribution: str) -> bool:
    """Whether ``package``'s server environment holds ``distribution``, read from disk."""
    return _holds(sorted(server_environment(stack, package).glob("lib/python*/site-packages")), distribution)


def image_holds(package: str) -> bool:
    """Whether ``package`` resolves in the image — the harness interpreter's own environment."""
    return _holds(None, package)


def plugin_absent(stack: TaiStack, package: str) -> bool:
    """Whether ``package`` is gone from every plugin environment of ``stack``: neither its
    plugin layer nor a server environment of its own holds it."""
    return not layer_holds(stack, package) and not server_environment(stack, package).parent.exists()


async def installed_payload(stack: TaiStack) -> dict[str, Any]:
    """The full installed-inventory body (``GET /api/marketplace/installed``):
    ``{"installed": [...]}``."""
    payload = await stack.api().get("/api/marketplace/installed")
    if not isinstance(payload, dict):
        raise AssertionError(f"/api/marketplace/installed did not return an object: {payload!r}")
    if not isinstance(payload.get("installed"), list):
        raise AssertionError(f"/api/marketplace/installed carries no 'installed' list: {payload!r}")
    return payload


async def installed_refs(stack: TaiStack) -> dict[str, dict[str, Any]]:
    """The attribution inventory keyed by ref (``GET /api/marketplace/installed``)."""
    return {row["ref"]: row for row in (await installed_payload(stack))["installed"]}


def persisted_manifest(stack: TaiStack) -> dict[str, Any]:
    """The stack's persisted ``manifest.yml`` on disk — the file the installer's
    config pipeline writes the merged manifest back to and a restart re-reads."""
    text = (stack.root / "config" / "manifest.yml").read_text(encoding="utf-8")
    manifest = yaml.safe_load(text)
    if not isinstance(manifest, dict):
        raise AssertionError(f"persisted manifest is not a mapping: {manifest!r}")
    return manifest


def manifest_mcp_titles(stack: TaiStack) -> set[str]:
    """The titles of the persisted manifest's ``mcp`` entries — the rows an
    mcp-server install appends and an uninstall removes."""
    entries = persisted_manifest(stack).get("mcp") or []
    return {entry["title"] for entry in entries}


async def _mcp_status(stack: TaiStack) -> dict[str, Any]:
    status = await stack.api().get("/api/mcp-status")
    if not isinstance(status, dict):
        raise AssertionError(f"/api/mcp-status did not return an object: {status!r}")
    return status


async def assert_mcp_server_not_failed(stack: TaiStack, title: str) -> None:
    """Assert ``GET /api/mcp-status`` does not list ``title`` as failed; a failure prints the whole status body."""
    status = await _mcp_status(stack)
    failed_titles = {row["title"] for row in (status.get("failed") or [])}
    assert title not in failed_titles, f"mcp server {title!r} is in the failed list: {status!r}"


async def wait_eta_mcp_server_live(stack: TaiStack, *, deadline: float = 45.0) -> None:
    """Wait until the mounted eta server is live (:func:`wait_eta_server_mount_live`)."""
    await wait_eta_server_mount_live(stack, title=ETA_MCP_TITLE, tool=ETA_MCP_TOOL, deadline=deadline)


async def wait_eta_server_mount_live(stack: TaiStack, *, title: str, tool: str, deadline: float = 45.0) -> None:
    """Wait until the mcp entry ``title``, which runs eta's one-tool server, is bound with its
    projected ``tool`` AND that tool answers eta's marker over ``/mcp``.

    A fresh MCP session per poll so a just-reloaded mount is observed. The mount is
    a spawned stdio child probed at reload, so this absorbs the spawn/probe tail."""

    async def live() -> bool:
        status = await _mcp_status(stack)
        bound = status.get("bound")
        if not isinstance(bound, dict):
            raise AssertionError(f"/api/mcp-status returned no 'bound' map: {status!r}")
        if tool not in set(bound.get(title) or []):
            return False
        async with stack.mcp() as mcp:
            if tool not in await mcp.tool_names():
                return False
            result = await mcp.call_tool(tool, {}, raise_on_error=False)
        if result.is_error:
            return False
        return probe_payload(result).get("eta") == "pong"

    await wait_for_async(live, deadline=deadline, message=f"mcp server {title!r} never went live with tool {tool!r}")


async def uninstall_and_assert_clean(
    stack: TaiStack, ref: str, *, package: str, tool_name: str, mcp_title: str | None = None, deadline: float = 30.0
) -> None:
    """The lifecycle clean-tail every install spec ends with: uninstall, then wait
    until the tool is gone (MCP + ``/api/tools``), the attribution row is gone, and
    no plugin environment of the stack holds the distribution.

    For an mcp-server install, pass ``mcp_title``: the mounted tool is gone the same
    way (the unmount deregisters it from MCP + ``/api/tools``), and the persisted
    manifest's ``mcp`` entry for that title must have been removed too — the row the
    install appended.

    The uninstall assertion is load-bearing — an uninstall rebuilds the plugin layer
    without the plugin, or deletes its server environment, and a spec that installed
    proves that happened."""
    await stack.api().post("/api/marketplace/uninstall", json={"ref": ref})
    await wait_tool_absent(stack, tool_name, deadline=deadline)

    async def gone() -> bool:
        if ref in await installed_refs(stack):
            return False
        if mcp_title is not None and mcp_title in manifest_mcp_titles(stack):
            return False
        return plugin_absent(stack, package)

    await wait_for_async(
        gone,
        deadline=deadline,
        message=f"{ref} never fully uninstalled (attribution row, manifest mcp entry, or plugin "
        f"environment holding {package!r} lingered)",
    )


# ---- tai plugins CLI drivers --------------------------------------------


def tai_bin() -> str:
    """The real venv ``tai`` console script, next to the interpreter."""
    candidate = Path(sys.executable).parent / "tai"
    if not candidate.exists():
        raise RuntimeError(f"tai console script not found next to interpreter at {candidate}")
    return str(candidate)


def cli_env(stack: TaiStack, home: Path) -> dict[str, str]:
    """A from-scratch child env pointing the CLI at the stack.

    ``TAI_API_KEY`` is set to an obviously fake constant so the key resolver never
    falls through to its interactive prompt (the stack runs auth-off, so any value
    is accepted); ``HOME`` is a throwaway dir so no real ``config.toml`` is read."""
    venv_bin = str(Path(sys.executable).parent)
    return {
        "PATH": os.pathsep.join([venv_bin, "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": str(home),
        "TAI_SERVER_URL": stack.origin(stack.port_a),
        "TAI_API_KEY": "sk-e2e-cli-not-a-real-key",
    }


def run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``tai --json plugins <args>`` and return the completed process."""
    return subprocess.run(
        [tai_bin(), "--json", "plugins", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def host_command_env(stack: TaiStack, home: Path) -> dict[str, str]:
    """The env an operator runs a host-side ``tai plugins`` command with: the env the stack's
    serve process runs with (its config mode and directory, its manifest, its deployment env),
    with ``home`` as its home and temp directory."""
    return {**child_env(stack, str(home), "serve"), "HOME": str(home)}


def run_rebuild(stack: TaiStack, home: Path) -> subprocess.CompletedProcess[str]:
    """Run the real ``tai plugins rebuild`` with the deployment's env, as an operator does."""
    return subprocess.run(
        [tai_bin(), "plugins", "rebuild"],
        env=host_command_env(stack, home),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def ok_json(proc: subprocess.CompletedProcess[str]) -> Any:
    """The parsed stdout of a CLI run asserted to have exited 0."""
    assert proc.returncode == 0, f"CLI failed (exit {proc.returncode}): {proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


# ---- plugin-compat readers ----------------------------------------------


def resolve_path(ref: str, contract: str | None = None) -> str:
    """The registry resolve path for ``ref``, with the core-aware ``contract``
    query pin when given."""
    path = f"/api/v1/plugins/{ref}/resolve"
    if contract is not None:
        path = f"{path}?contract={quote(contract)}"
    return path


def outcomes_by_ref(payload: Any) -> dict[str, dict[str, Any]]:
    """The per-ref outcome rows of an upgrade-all response, keyed by ref."""
    rows = payload["results"] if isinstance(payload, dict) else payload
    assert isinstance(rows, list), f"upgrade-all reported no per-ref outcome list: {payload!r}"
    keyed: dict[str, dict[str, Any]] = {}
    for row in rows:
        assert isinstance(row, dict), f"upgrade-all outcome row is not an object: {row!r}"
        assert isinstance(row.get("ref"), str), f"upgrade-all outcome row carries no ref: {row!r}"
        keyed[row["ref"]] = row
    return keyed


def compat_block(row: dict[str, Any]) -> dict[str, Any]:
    """The per-row ``compat`` verdict (``{"status", "reason"}``) of an
    installed-inventory row."""
    compat = row.get("compat")
    assert isinstance(compat, dict), f"installed row for {row.get('ref')!r} carries no compat block: {row!r}"
    return compat
