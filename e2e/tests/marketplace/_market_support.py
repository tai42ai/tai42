"""Shared drivers for the marketplace specs.

The install → live-tool → uninstall arc is the area's core promise and is
exercised by several specs (install lifecycle, update, advisories, CLI parity),
so its waits and its clean-tail assertions live here once. Every wait is a
:func:`~tai42_e2e.waiting.wait_for_async` over a real observable — a tool answering
over MCP, a row in the attribution store, a distribution resolving in the venv —
never a sleep.

The ``tai plugins`` CLI drivers (the real venv ``tai`` binary against a stack)
and the plugin-compat response readers (the resolve path with a ``contract``
pin, the per-ref upgrade-all outcomes, an installed row's compat/quarantined
blocks) live here too, shared by the CLI-parity and compat specs.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastmcp.client.client import CallToolResult

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


def distribution_absent(package: str) -> bool:
    """Whether ``package`` no longer resolves in the venv (caches invalidated so
    the read reflects a just-completed pip uninstall)."""
    importlib.invalidate_caches()
    try:
        importlib.metadata.distribution(package)
    except importlib.metadata.PackageNotFoundError:
        return True
    return False


async def installed_payload(stack: TaiStack) -> dict[str, Any]:
    """The full installed-inventory body (``GET /api/marketplace/installed``):
    ``{"installed": [...], "quarantined": [{"name", "reason"}, ...]}``."""
    payload = await stack.api().get("/api/marketplace/installed")
    if not isinstance(payload, dict):
        raise AssertionError(f"/api/marketplace/installed did not return an object: {payload!r}")
    if not isinstance(payload.get("installed"), list):
        raise AssertionError(f"/api/marketplace/installed carries no 'installed' list: {payload!r}")
    return payload


async def installed_refs(stack: TaiStack) -> dict[str, dict[str, Any]]:
    """The attribution inventory keyed by ref (``GET /api/marketplace/installed``)."""
    return {row["ref"]: row for row in (await installed_payload(stack))["installed"]}


def quarantined_by_name(payload: dict[str, Any]) -> dict[str, str]:
    """The installed body's boot-quarantine entries, name → reason."""
    entries = payload.get("quarantined")
    assert isinstance(entries, list), f"installed body carries no quarantined list: {payload!r}"
    keyed: dict[str, str] = {}
    for entry in entries:
        assert isinstance(entry, dict), f"quarantined entry is not an object: {entry!r}"
        keyed[entry["name"]] = entry["reason"]
    return keyed


async def uninstall_and_assert_clean(
    stack: TaiStack, ref: str, *, package: str, tool_name: str, deadline: float = 30.0
) -> None:
    """The lifecycle clean-tail every install spec ends with: uninstall, then wait
    until the tool is gone (MCP + ``/api/tools``), the attribution row is gone, and
    the distribution no longer resolves in the venv.

    The uninstall assertion is load-bearing — ``pip install`` mutated the ONE
    shared venv, so a spec that installed must prove it cleaned up (the venv guard
    would otherwise raise for the whole area)."""
    await stack.api().post("/api/marketplace/uninstall", json={"ref": ref})
    await wait_tool_absent(stack, tool_name, deadline=deadline)

    async def gone() -> bool:
        if ref in await installed_refs(stack):
            return False
        return distribution_absent(package)

    await wait_for_async(
        gone,
        deadline=deadline,
        message=f"{ref} never fully uninstalled (attribution row or venv distribution {package!r} lingered)",
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
        "TAI_SERVER_URL": f"http://{stack.host}:{stack.port_a}",
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
