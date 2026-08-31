"""``tai42-e2e-studio-stack`` — boot the studio_stack on a fixed port for Playwright.

The ``ui/`` Playwright suite's ``webServer`` runs this console script: it boots
the ``build_studio_stack`` profile (the real built Studio served through a
multi-worker skeleton with access control ON) on a KNOWN app port so
``webServer.url`` is knowable up front, prints a ready line, then blocks until
SIGTERM/SIGINT and runs the harness's leak-checked teardown. It reuses the one
shared boot/seed/allocation implementation in :mod:`tai42_e2e.harness`.

Env knobs (``TAI_E2E_`` space, all with test-only defaults):
  ``TAI_E2E_UI_PORT``          the pinned Studio app port / browser origin (8770)
  ``TAI_E2E_UI_LLM_PORT``      the pinned scripted-LLM control port (8771)
  ``TAI_E2E_UI_API_KEY``       the seeded root key Playwright pastes at /login
  ``TAI_E2E_STUDIO_DIST_PATH`` the built Studio dist (defaults to the sibling
                               ``tai-studio/apps/studio/dist`` checkout)
  ``TAI_E2E_STUDIO_STACK``     ``<module>:<callable>`` of an external stack builder
                               (``(resources, variants) -> StackConfig``) to boot
                               instead of the OSS ``build_studio_stack``; lets a
                               product ship its own studio profile through this runner
  ``TAI_E2E_STUDIO_CHECKPOINT_DB`` allocate a module-capable checkpoint Redis DB for
                               the stack (an external profile needing langgraph checkpointing)
plus the shared ``TAI_E2E_REDIS_URL`` / ``TAI_E2E_PG_*`` infra coordinates.

When ``TAI_E2E_MARKETPLACE`` is on, the runner additionally forges the fixture
wheels, boots the tai42-marketplace registry (on the pinned
``TAI_E2E_UI_MP_PORT``, admin token ``TAI_E2E_UI_MP_ADMIN_TOKEN``), seeds the
fixture catalog, wires the studio stack at it, and pnpm-builds + serves the
public ``../tai-marketplace/web`` site on ``TAI_E2E_UI_MP_WEB_PORT`` with its
``/api`` proxy pointed at the registry. When off, none of that runs and the
runner is behaviorally identical.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from tai42_e2e import ports
from tai42_e2e.harness import allocate_resources, connect_infra, release_resources, seed_studio_auth
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.manifests import build_studio_stack
from tai42_e2e.marketplace import (
    MarketplaceService,
    declared_routes_dispatch_failure,
    forge_fixture_artifacts,
    registry_supports_declared_routes,
    seed_epsilon_listing,
    seed_fixture_catalog,
)
from tai42_e2e.netfixtures import OAuthIdp
from tai42_e2e.pkgsource import FixturePackageIndex
from tai42_e2e.procs import ProcessHandle
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import Infra, StackConfig, StackResources, TaiStack
from tai42_e2e.variants import Variants
from tai42_e2e.waiting import WaitTimeout, wait_for


class StudioRunnerSettings(BaseSettings):
    """The pinned ports + seeded key + dist path the browser-e2e runner needs.
    Read from the ``TAI_E2E_`` env space alongside :class:`HarnessSettings`."""

    model_config = SettingsConfigDict(env_prefix="TAI_E2E_", extra="ignore")

    ui_port: int = 8770
    ui_llm_port: int = 8771
    # The pinned stub-IdP port: the connectors spec flips its denial knob over
    # HTTP at this known origin (the OAuth connect flow reaches it server-side).
    ui_idp_port: int = 8772
    # An obviously test-only key. NOT for production: the stack seeds it with a
    # wildcard scope, which a real deployment never does.
    ui_api_key: str = "sk-e2e-ui-DO-NOT-USE-IN-PRODUCTION-000"
    studio_dist_path: str | None = None
    # An external stack builder ``<module>:<callable>`` to boot instead of the OSS
    # ``build_studio_stack``; the callable takes ``(resources, variants)`` and returns
    # a ``StackConfig``. Lets a product outside this repo drive the runner with its own
    # studio profile. When unset, the OSS profile boots.
    studio_stack: str | None = None
    # Reserve a module-capable checkpoint Redis DB for the stack (an external profile
    # whose engine needs langgraph checkpointing). Bundle/prerequisite freshness for such
    # a profile is that external builder's responsibility.
    studio_checkpoint_db: bool = False
    # The pinned marketplace coordinates the browser specs address directly: registry port,
    # public site port, and registry admin token. Only consulted when TAI_E2E_MARKETPLACE is
    # on. These defaults MUST match the ``ui/tests/helpers.ts`` constants.
    ui_mp_port: int = 8778
    ui_mp_web_port: int = 8779
    ui_mp_admin_token: str = "mp-admin-e2e-DO-NOT-USE-IN-PRODUCTION-000"


def _resolve_dist_path(runner: StudioRunnerSettings) -> Path:
    """The built Studio dist to serve. Explicit ``TAI_E2E_STUDIO_DIST_PATH`` wins;
    otherwise the sibling ``tai-studio`` checkout. Fails LOUDLY with the build
    hint if ``index.html`` is missing — never a silent 404 page."""
    if runner.studio_dist_path is not None:
        dist = Path(runner.studio_dist_path)
    else:
        repo_root = Path(__file__).resolve().parents[2]
        dist = repo_root.parent / "tai-studio" / "apps" / "studio" / "dist"
    if not (dist / "index.html").is_file():
        raise RuntimeError(
            f"Studio dist not found at {dist} (no index.html). Build it first: "
            "`pnpm --filter @tai42/studio-app run build` in the tai-studio checkout, "
            "or set TAI_E2E_STUDIO_DIST_PATH."
        )
    return dist


def _print_ready(url: str, api_key: str) -> None:
    print(f"tai42-e2e-studio-stack ready at {url} (test-only api key: {api_key})", flush=True)


def _pids_listening_on(port: int) -> set[int]:
    """PIDs holding a LISTEN socket on the loopback ``port``, via ``/proc``.

    Maps the port to its listening socket inodes from ``/proc/net/tcp[6]`` (state
    ``0A`` = LISTEN), then finds every process whose ``/proc/<pid>/fd`` points at a
    ``socket:[inode]`` for one of them. Pure ``/proc``, no external tool or
    dependency — the same read-only introspection the harness already relies on."""
    inodes: set[str] = set()
    for proc_net in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(proc_net).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10:
                continue
            local_addr, state, inode = fields[1], fields[3], fields[9]
            if state != "0A":  # 0A == TCP_LISTEN
                continue
            if int(local_addr.rsplit(":", 1)[1], 16) == port:
                inodes.add(inode)
    if not inodes:
        return set()
    targets = {f"socket:[{inode}]" for inode in inodes}
    holders: set[int] = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # process gone or not ours to inspect
        for fd in fds:
            with contextlib.suppress(OSError):
                if os.readlink(f"{fd_dir}/{fd}") in targets:
                    holders.add(int(entry))
                    break
    return holders


def _free_pinned_ports(pinned: list[int]) -> None:
    """Reap any process still LISTENing on a pinned studio port before boot.

    A clean shutdown tears the stack down on SIGTERM and frees these ports, but a
    hard kill (SIGKILL before teardown could finish, a crash) can strand a stack
    session-leader bound to the fixed browser / LLM-stub / IdP ports — and the next
    boot would then fail to bind (or a reused-server run would serve the stale,
    degraded stack). Free them deterministically with the harness's own reap
    discipline (SIGTERM, then SIGKILL any that linger) and confirm release, so every
    run starts against a clean slate regardless of how a PRIOR run ended."""
    holders: set[int] = set()
    for port in pinned:
        holders |= _pids_listening_on(port)
    if not holders:
        return
    print(f"tai42-e2e-studio-stack: reaping {len(holders)} process(es) holding pinned ports {pinned}", flush=True)

    def _all_free() -> bool:
        return all(ports.is_free(p) for p in pinned)

    for pid in holders:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    with contextlib.suppress(WaitTimeout):
        wait_for(_all_free, deadline=10.0)
    if not _all_free():
        for pid in holders:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        wait_for(
            _all_free,
            deadline=5.0,
            message=(
                f"pinned studio ports {pinned} still bound after SIGKILLing their holders; a foreign "
                "process is holding them and boot cannot claim the fixed browser origin"
            ),
        )


def _resolve_stack_builder(runner: StudioRunnerSettings) -> Callable[[StackResources, Variants], StackConfig]:
    """The stack-profile builder the runner boots. Defaults to the OSS
    ``build_studio_stack``; ``TAI_E2E_STUDIO_STACK=<module>:<callable>`` selects an
    external builder with the same ``(resources, variants) -> StackConfig`` contract,
    so a product shipping its own studio profile drives this runner with no harness
    edit. Any bundle-freshness / prerequisite check the external profile needs is that
    builder's own responsibility. Import or lookup failure raises loudly."""
    if runner.studio_stack is None:
        return build_studio_stack
    module_name, sep, attr = runner.studio_stack.partition(":")
    if not sep or not module_name or not attr:
        raise RuntimeError(f"TAI_E2E_STUDIO_STACK must be '<module>:<callable>', got {runner.studio_stack!r}")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


class _MarketplaceWebSite:
    """The public marketplace web site (the tai-marketplace monorepo's web/
    workspace), pnpm-built from the sibling checkout and served with ``vite
    preview`` on a pinned port.

    ``VITE_MP_API_BASE_URL`` is left UNSET at build time so the browser client
    fetches the registry SAME-ORIGIN at ``/api``; the preview proxy (which
    defaults to the committed vite config's ``server.proxy``) then forwards
    ``/api`` to ``VITE_MP_API_PROXY_TARGET``, the running registry's base URL."""

    def __init__(self, repo: Path, *, port: int, proxy_target: str, log_dir: Path) -> None:
        self._repo = repo
        self.port = port
        self._proxy_target = proxy_target
        self._log_dir = log_dir
        self._proc: ProcessHandle | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _node_env(self) -> dict[str, str]:
        """The pnpm/node toolchain env. Inherits the runner's PATH/HOME (node and
        pnpm live on it) with the two VITE knobs overlaid: the build-time base URL
        cleared so the client stays same-origin, and the preview proxy pointed at
        the registry."""
        env = dict(os.environ)
        env.pop("VITE_MP_API_BASE_URL", None)
        env["VITE_MP_API_PROXY_TARGET"] = self._proxy_target
        return env

    def build(self) -> None:
        """``pnpm install --frozen-lockfile`` then build the web app. A non-zero
        exit raises loudly with the captured output."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / "mp-web-build.log"
        env = self._node_env()
        for step in (
            ["pnpm", "install", "--frozen-lockfile"],
            # The repo's own recursive build: the web app consumes the built dist
            # of its workspace packages (@mp/api-client, @mp/ui-kit), so they must
            # build first — `pnpm -r build` orders that.
            ["pnpm", "run", "build"],
        ):
            proc = subprocess.run(step, cwd=str(self._repo), env=env, capture_output=True, text=True, check=False)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"$ {' '.join(step)}\n{proc.stdout}\n{proc.stderr}\n")
            if proc.returncode != 0:
                raise RuntimeError(
                    f"marketplace web {' '.join(step)} failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
                )

    def start(self) -> None:
        """Serve the built dist with ``vite preview`` on the pinned port and wait
        for it to answer, reporting the log tail on an early exit."""
        argv = [
            "pnpm",
            "--filter",
            "@mp/web",
            "exec",
            "vite",
            "preview",
            "--port",
            str(self.port),
            "--host",
            "127.0.0.1",
            "--strictPort",
        ]
        self._proc = ProcessHandle(
            name="mp-web",
            argv=argv,
            cwd=self._repo,
            env=self._node_env(),
            log_path=self._log_dir / "mp-web.log",
        )
        self._proc.start()

        def probe() -> bool:
            proc = self._proc
            if proc is not None and not proc.is_running():
                raise RuntimeError(f"marketplace web preview exited early (code {proc.poll()}):\n{proc.log_tail()}")
            try:
                return httpx.get(self.base_url, timeout=2.0).status_code == 200
            except httpx.HTTPError:
                return False

        wait_for(probe, deadline=30.0, message=f"marketplace web never became ready at {self.base_url}")

    def teardown(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            if self._proc.is_running():
                raise RuntimeError("marketplace web preview still running after SIGKILL (leak)")
            self._proc = None
        wait_for(lambda: ports.is_free(self.port), deadline=5.0, message=f"web port {self.port} never freed")


@dataclass
class _MarketplaceBundle:
    """The marketplace surfaces the browser leg boots alongside the studio stack:
    the package index, the registry service, and the public web site. Torn down
    together after the stack (whose advisories poll targets the registry)."""

    index: FixturePackageIndex
    service: MarketplaceService
    website: _MarketplaceWebSite

    def teardown(self) -> None:
        """Reap the web site, then the registry, then the index — each guarded so
        one failure never hides another, all aggregated into one raise."""
        errors: list[str] = []
        for label, fn in (
            ("web site", self.website.teardown),
            ("registry", self.service.teardown),
            ("package index", self.index.stop),
        ):
            try:
                fn()
            except Exception as exc:
                errors.append(f"{label}: {exc!r}")
        if errors:
            raise RuntimeError("marketplace bundle teardown found errors:\n  " + "\n  ".join(errors))


def _resolve_web_repo() -> Path:
    """The public site's sibling checkout. Fails loudly with the clone hint when
    it is missing."""
    repo_root = Path(__file__).resolve().parents[2]
    web = repo_root.parent / "tai-marketplace" / "web"
    if not (web / "package.json").is_file():
        raise RuntimeError(
            f"marketplace web workspace not found at {web} (no package.json); "
            "clone tai-marketplace as a sibling of tai-e2e for the marketplace browser leg."
        )
    return web


def _start_marketplace(infra: Infra, root: Path, runner: StudioRunnerSettings) -> _MarketplaceBundle:
    """Forge the fixture wheels, boot the registry on the pinned port + admin
    token, seed the fixture catalog, and pnpm-build + serve the public site with
    its ``/api`` proxy pointed at the registry. On any failure the partial
    allocation is reaped before the error propagates."""
    artifacts_dir = root / "mp-artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifacts = forge_fixture_artifacts(artifacts_dir)
    index = FixturePackageIndex()
    index.start()
    service: MarketplaceService | None = None
    website: _MarketplaceWebSite | None = None
    try:
        service = MarketplaceService(
            infra,
            root / "mp-registry",
            index_url=index.url,
            port=runner.ui_mp_port,
            admin_token=runner.ui_mp_admin_token,
        )
        service.boot()
        asyncio.run(seed_fixture_catalog(service, index, artifacts))
        # The browser leg's registry is its own (not the pytest router-merge one), so
        # the router/middleware fixture is safe to add to THIS browse catalog: it lets
        # the marketplace-web spec assert a router/middleware listing browses + filters
        # while the shared pytest catalog stays alpha/beta/gamma. Its kinds are neither
        # tool nor extension, so it never disturbs the existing browse assertions.
        #
        # Epsilon carries a declared `routes` block (contract 2.0's PluginSpec field).
        # The resolved registry ref runs tai42-contract 4.x, which still carries that
        # `routes` field, so the registry accepts `routes` at seed time and this seed
        # RUNS, mirroring the pytest route legs' gate. The gate
        # only degrades to a skipped seed if the registry venv is not yet built; when a
        # ref is DISPATCHED (TAI_E2E_MARKETPLACE_REF), a False gate is escalated to a
        # hard failure with the dual-cause detail (ordering bug vs contract-floor
        # regression), never a silent skip of the browser fixture.
        if registry_supports_declared_routes():
            asyncio.run(seed_epsilon_listing(service, index, artifacts))
        else:
            detail = declared_routes_dispatch_failure()
            if detail is not None:
                raise RuntimeError(detail)
        website = _MarketplaceWebSite(
            _resolve_web_repo(),
            port=runner.ui_mp_web_port,
            proxy_target=service.base_url,
            log_dir=root / "logs",
        )
        website.build()
        website.start()
        return _MarketplaceBundle(index=index, service=service, website=website)
    except BaseException:
        if website is not None:
            with contextlib.suppress(Exception):
                website.teardown()
        if service is not None:
            with contextlib.suppress(Exception):
                service.teardown()
        with contextlib.suppress(Exception):
            index.stop()
        raise


def main() -> None:
    settings = HarnessSettings()
    runner = StudioRunnerSettings()
    dist = _resolve_dist_path(runner)
    # TAI_E2E_STUDIO_STACK selects an external stack profile over the OSS one; unset
    # boots build_studio_stack.
    build_stack = _resolve_stack_builder(runner)

    # Free the fixed browser / LLM-stub / IdP ports before booting: a prior hard-killed run
    # can leave a session-leader bound on the exact ports this stack claims. Under the
    # marketplace gate the registry + public-site ports are pinned too, so reap them the same.
    pinned_ports = [runner.ui_port, runner.ui_llm_port, runner.ui_idp_port]
    if settings.marketplace:
        pinned_ports += [runner.ui_mp_port, runner.ui_mp_web_port]
    _free_pinned_ports(pinned_ports)

    infra = connect_infra(settings)
    stub = LlmStub(port=runner.ui_llm_port)
    stub.start()
    # The stub OAuth IdP the connectors spec drives: pinned so the spec can flip
    # its denial knob over HTTP, and reachable server-side by the connect flow.
    idp = OAuthIdp(port=runner.ui_idp_port)
    idp.start()
    # Root the stack dir inside the repo so a CI failure run (TAI_E2E_KEEP_STACKS)
    # keeps the per-process logs where the ``tai-e2e/**/tai-e2e-studio-*/`` upload
    # glob can find them; a system-tmp dir would fall outside the workspace.
    root = Path(tempfile.mkdtemp(prefix="tai-e2e-studio-", dir=Path(__file__).resolve().parents[2]))
    stop = threading.Event()

    def _handle_signal(signum: int, frame: object) -> None:
        _ = frame
        print(f"tai42-e2e-studio-stack received signal {signum}; tearing down", flush=True)
        stop.set()

    # Only booted under the opt-in gate; when off, this stays None and the studio
    # stack is wired exactly as it is today (marketplace_url/package_index_url unset).
    mp_bundle: _MarketplaceBundle | None = None
    try:
        # Boot the registry + public site BEFORE allocating the stack's resources,
        # so the stack can be wired at them. Torn down in the outer finally, after
        # the stack (whose advisories poll targets the registry).
        if settings.marketplace:
            mp_bundle = _start_marketplace(infra, root, runner)
        mp_kwargs: dict[str, Any] = (
            {"marketplace_url": mp_bundle.service.base_url, "package_index_url": mp_bundle.index.url}
            if mp_bundle is not None
            else {}
        )
        resources = allocate_resources(
            infra,
            root,
            # TAI_E2E_STUDIO_CHECKPOINT_DB reserves a module-capable Redis DB for an
            # external profile whose engine needs langgraph checkpointing.
            allocate_checkpoint_db=runner.studio_checkpoint_db,
            llm_base_url=stub.base_url,
            gh_webhook_secret=secrets.token_hex(16),
            studio_dist_path=str(dist),
            idp_base_url=idp.base_url,
            # The connector crypto settings decode these with STANDARD base64
            # (the KEK to exactly 32 bytes), so mint padded standard-base64 keys.
            connectors_kek=base64.b64encode(secrets.token_bytes(32)).decode(),
            connectors_state_hmac_key=base64.b64encode(secrets.token_bytes(32)).decode(),
            **mp_kwargs,
        )
        try:
            config = build_stack(resources, infra.variants)
        except BaseException:
            # A builder raise (a missing prerequisite) must not leak the Redis index,
            # the Postgres clone and the broker lease just allocated.
            release_resources(infra, resources)
            raise
        stack = TaiStack(config, infra, resources, root, app_port=runner.ui_port)
        try:
            # The readiness probes hit /health and /metrics, which access control
            # denies until the route table pins them public, so the seed must land
            # before the processes answer their first request.
            seed_studio_auth(infra, resources, api_key=runner.ui_api_key)
            # Readiness drains the boot reload gate through the MCP probe, which this
            # access-controlled stack fences; the probe runs under the seeded root key.
            stack.auth_token = runner.ui_api_key
            signal.signal(signal.SIGTERM, _handle_signal)
            signal.signal(signal.SIGINT, _handle_signal)
            stack.boot()
            _print_ready(f"http://{stack.host}:{stack.port_a}", runner.ui_api_key)
            stop.wait()
        finally:
            stack.teardown()
    finally:
        # Tear down every surface even if one raises, aggregating the failures so
        # none is hidden. The marketplace bundle goes first (the stack is already
        # down, so its poll no longer targets the registry).
        teardowns: list[tuple[str, Callable[[], object]]] = []
        if mp_bundle is not None:
            teardowns.append(("marketplace", mp_bundle.teardown))
        teardowns += [("idp", idp.stop), ("llm stub", stub.stop), ("redis", infra.redis.close)]
        errors: list[str] = []
        for label, fn in teardowns:
            try:
                fn()
            except Exception as exc:
                errors.append(f"{label}: {exc!r}")
        if errors:
            raise RuntimeError("studio runner teardown found errors:\n  " + "\n  ".join(errors))


if __name__ == "__main__":
    sys.exit(main())
