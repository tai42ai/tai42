"""A stdio MCP server launched by a package runner binds its tools through the config API.

Each case adds an MCP entry whose command is a bare package runner (``uvx``, ``npx``)
naming a public package at an exact version, then reads ``GET /api/tools`` until the
server's tools are bound under the entry's title, reloads the server and removes the
entry. The entry's own ``env`` points the runner's cache (``npm_config_cache`` for
``npx``, ``UV_CACHE_DIR`` for ``uvx``) at a directory new to this case, so every run is
the package's first start and no run reads or writes the cache of the host or of the
stack's user. The runner fetches the package on that first start; a start that outlasts
the reload's wait keeps running up to the start budget and binds when it completes, so
while the read waits the title is listed failed only as ``starting``: any other
failure fails the case at once. The reload after the bind starts the server again
from that cache, so it reports ``ok`` only when no start was torn down mid-fetch.

The cache directory lives on the server's filesystem: a built stack's children share
this host's, so it is the case's own ``tmp_path``; a target's server is elsewhere, so
it is a new directory under the target's ``/tmp``, and the case removes it through the
stack once the entry is gone: it adds a stdio entry whose command is ``rm -rf`` of that
directory, waits until every worker records that entry's start as ended (``rm`` lists
no tools, so it is listed failed), and removes the entry.

A built stack's children run on the harness's fixed ``PATH``; the profile appends the
host's ``uvx`` and ``npx`` dirs to it, so the host carries both runners.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from tai42_e2e import Infra, StackConfig, StackResources, wait_for_async
from tai42_e2e.booting import boot_stack
from tai42_e2e.child_env import child_path
from tai42_e2e.manifests import build_core_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.variants import Variants
from tai42_e2e.waiting import WaitTimeoutError

pytestmark = pytest.mark.needs("mutable", "package-runners")

_RUNNERS = ("uvx", "npx")

# The bind wait covers the default 15 s start budget, with room for the reload that
# writes the entry.
_BIND_S = 60.0


@dataclass(frozen=True)
class _RunnerServer:
    """A public MCP server package started by a package runner, and the served names (lower
    case, ``-`` turned into ``_``) of tools it advertises."""

    command: str
    args: tuple[str, ...]
    tools: frozenset[str]
    # The env var naming the runner's cache directory.
    cache_env: str


_SERVERS = {
    "uvx": _RunnerServer(
        "uvx", ("mcp-server-time==2026.10.28",), frozenset({"get_current_time", "convert_time"}), "UV_CACHE_DIR"
    ),
    "npx": _RunnerServer(
        "npx",
        ("--yes", "@modelcontextprotocol/server-everything@1.0.0"),
        frozenset({"echo", "get_sum"}),
        "npm_config_cache",
    ),
}


def _runner_dirs() -> list[str]:
    """The host dirs holding the package runners, in :data:`_RUNNERS` order, each once."""
    dirs: list[str] = []
    for runner in _RUNNERS:
        found = shutil.which(runner)
        if found is None:
            raise RuntimeError(f"a built stack for the package-runner servers needs `{runner}` on the host PATH")
        parent = str(Path(found).parent)
        if parent not in dirs:
            dirs.append(parent)
    return dirs


def _build_package_runner_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The core profile with the host's package runners on its children's ``PATH``."""
    base = build_core_stack(res, variants)
    return replace(base, name="package-runners", env={**base.env, "PATH": child_path(*_runner_dirs())})


@pytest.fixture(scope="module")
def runner_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from boot_stack(infra, tmp_path_factory.mktemp("package-runners"), _build_package_runner_stack)


async def _remove_on_server(stack: TaiStack, path: str, title: str) -> None:
    """Remove ``path`` on the server's filesystem: a stdio entry ``title`` runs ``rm -rf path``.

    Returns once every worker lists ``title`` failed with its start ended, then removes the entry.
    """
    api = stack.api()
    entry = {"title": title, "config": {"command": "rm", "args": ["-rf", path]}}
    await api.post("/api/mcp-config/entries", json={"entries": [entry]}, retry_on_reloading=True)
    try:

        async def ended() -> bool:
            failed = await api.get("/api/mcp-status/failed", retry_on_reloading=True)
            # One result per worker: each runs the entry's command on its own filesystem.
            return bool(failed["results"]) and all(
                any(row["title"] == title and row["category"] != "starting" for row in result["payload"])
                for result in failed["results"]
            )

        await wait_for_async(ended, deadline=_BIND_S, interval=1.0, message=f"{title} never ran `rm -rf {path}`")
    finally:
        await api.delete(f"/api/mcp-config/entries/{title}", retry_on_reloading=True)


@pytest.fixture
async def runner_cache(
    infra: Infra, runner_stack: TaiStack, tmp_path: Path, uniq: Callable[[str], str]
) -> AsyncIterator[str]:
    """A runner cache directory new to this case, on the server's filesystem, removed after it."""
    if infra.target is None:
        yield str(tmp_path / "runner-cache")
        return
    cache = f"/tmp/{uniq('tai-e2e-runner-cache')}"
    yield cache
    await _remove_on_server(runner_stack, cache, uniq("runnercacheremoval"))


# The stack boot inside the first test's timer (120) + the bind wait + the reload + the entry's delete.
@pytest.mark.timeout(300)
@pytest.mark.parametrize("runner", _RUNNERS)
async def test_a_package_runner_server_binds_its_tools(
    runner_stack: TaiStack, runner: str, runner_cache: str, uniq: Callable[[str], str]
) -> None:
    server = _SERVERS[runner]
    title = uniq(f"{runner}server")
    expected = {f"{title}_{tool}" for tool in server.tools}
    api = runner_stack.api()
    entry = {
        "title": title,
        "config": {"command": server.command, "args": list(server.args), "env": {server.cache_env: runner_cache}},
    }
    await api.post("/api/mcp-config/entries", json={"entries": [entry]}, retry_on_reloading=True)
    try:

        async def bound() -> bool:
            names = set(await api.get("/api/tools", retry_on_reloading=True))
            if expected <= names:
                return True
            failed = await api.get("/api/mcp-status/failed", retry_on_reloading=True)
            torn = [
                entry
                for row in failed["results"]
                for entry in row["payload"]
                if entry["title"] == title and entry["category"] != "starting"
            ]
            assert not torn, f"{title}'s first start failed instead of running on to bind: {torn}"
            return False

        try:
            await wait_for_async(bound, deadline=_BIND_S, interval=1.0, message=f"{title} never bound {expected}")
        except WaitTimeoutError as exc:
            failed = await api.get("/api/mcp-status/failed", retry_on_reloading=True)
            raise AssertionError(f"{exc}; the failed servers: {failed}") from exc
        report = await api.post(f"/api/mcp-status/{title}/reload", json={}, retry_on_reloading=True)
        assert report["reachable"] is True, report
        assert all(row["payload"]["status"] == "ok" for row in report["results"]), report
    finally:
        await api.delete(f"/api/mcp-config/entries/{title}", retry_on_reloading=True)
