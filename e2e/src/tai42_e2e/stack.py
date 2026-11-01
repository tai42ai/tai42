"""The boot/teardown engine.

A :class:`TaiStack` stages a config dir, allocates ports, spawns the REAL
``tai`` entrypoints (``serve`` / ``backend worker`` / ``metrics``) as OS
processes with a clean env, waits for readiness over HTTP + the app-owned
worker-bus presence census, and tears everything down leak-free. It never imports the
system under test into this process and never talks to Docker.

Per-stack metrics-dir isolation rides on ``TMPDIR``: the skeleton's
``MetricsSettings.prometheus_multiproc_dir`` defaults to
``<tempfile.gettempdir()>/tai42_prometheus``, so pointing a process's ``TMPDIR`` at a
per-run-family dir gives that family its own multiproc dir without the harness ever
setting ``PROMETHEUS_MULTIPROC_DIR`` — stamping that env var is the entrypoint's own job.
The harness asserts it never sets it (see :func:`tai42_e2e.child_env.child_env`).

The process spawning, child-env construction, readiness waits, port-ownership heal and
restarts live in the :mod:`~tai42_e2e.spawning`, :mod:`~tai42_e2e.child_env`,
:mod:`~tai42_e2e.readiness`, :mod:`~tai42_e2e.port_ownership` and
:mod:`~tai42_e2e.restarts` modules as free functions this class drives; the
respawn-on-exit supervisor is :class:`~tai42_e2e.supervisor.RespawnSupervisor`; the frozen
config/resource/infra descriptions live in :mod:`~tai42_e2e.topology`."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from tai42_e2e import child_env, port_ownership, ports, readiness, restarts, spawning
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.mcp import McpClient, mcp_read_timeout
from tai42_e2e.metrics import Scrape, scrape
from tai42_e2e.supervisor import RespawnSupervisor
from tai42_e2e.topology import Infra, StackConfig, StackResources, Topology, _ProcSpec
from tai42_e2e.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from tai42_e2e.procs import ProcessHandle
    from tai42_e2e.tcprelay import TcpRelay
    from tai42_e2e.variants import BusWorker


class TaiStack:
    """A booted stack. Construct with a config + infra + allocated resources,
    then use as a context manager (the pytest fixtures do this)."""

    def __init__(
        self,
        config: StackConfig,
        infra: Infra,
        resources: StackResources,
        root: Path,
        *,
        app_port: int | None = None,
    ) -> None:
        self.config = config
        self.infra = infra
        self.resources = resources
        self.root = root
        self.host = "127.0.0.1"
        self.auth_token: str | None = None
        # A caller-pinned primary app port (the Studio origin a Playwright
        # webServer.url points at). Only a single-app-port topology can honour
        # it — REPLICAS would need two known ports; boot enforces that.
        self._pinned_app_port = app_port

        self.app_ports: list[int] = []
        self.metrics_port: int | None = None
        self.procs: dict[str, ProcessHandle] = {}
        self.specs: dict[str, _ProcSpec] = {}
        self.allocated_ports: list[int] = []
        # Per-stack TCP relays (the infra-outage tests front Redis/PG with one).
        # Attached before boot; stopped and leak-checked in teardown like a port.
        self._relays: list[TcpRelay] = []
        # The main run family's multiproc dir (MULTIWORKER: the shared dir;
        # REPLICAS: replica A's dir, which the backend + metrics join).
        self.metrics_dir: str = ""
        self.config_dir = root / "config"
        self.logs_dir = root / "logs"
        # One backend liveness file per child process (``child_env``).
        self.liveness_dir = root / "liveness"
        # Respawn-on-exit supervisor (opt-in ``config.supervised``); its lock serialises every
        # ``procs``/``specs`` mutation the supervisor and teardown/restart race on.
        self.supervisor = RespawnSupervisor(self)

    # ---- lifecycle -------------------------------------------------------

    def __enter__(self) -> TaiStack:
        try:
            self.boot()
        except BaseException:
            self.teardown()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.teardown()

    def render_config(self) -> Path:
        """Write the deployment's config directory: the manifest and the stored env file. A
        host-side command run before the first boot reads them as a server does. Returns the
        manifest path."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.config_dir / "manifest.yml"
        # Seed the manifest verbatim when a raw document is supplied (the
        # comment-preservation scenario), else dump the config dict.
        if self.config.raw_manifest is not None:
            manifest_path.write_text(self.config.raw_manifest, encoding="utf-8")
        else:
            manifest_path.write_text(yaml.safe_dump(self.config.manifest, sort_keys=False), encoding="utf-8")
        child_env.render_env_file(self)
        return manifest_path

    def boot(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.liveness_dir.mkdir(parents=True, exist_ok=True)

        n_app = 2 if self.config.topology is Topology.REPLICAS else 1
        if self._pinned_app_port is not None:
            if n_app != 1:
                raise RuntimeError(
                    "a pinned app port requires a single-app-port topology (MULTIWORKER); REPLICAS binds two app ports"
                )
            ports.reserve_specific_port(self._pinned_app_port)
            self.app_ports = [self._pinned_app_port]
        else:
            self.app_ports = [ports.allocate_port() for _ in range(n_app)]
        self.allocated_ports.extend(self.app_ports)
        if self.config.run_metrics:
            self.metrics_port = ports.allocate_port()
            self.allocated_ports.append(self.metrics_port)

        manifest_path = self.render_config()

        # One run family per metrics dir. REPLICAS: replica A + backend + metrics
        # share family "a"; replica B is family "b" with no scraper.
        family_dirs = spawning.make_family_dirs(self.root, n_app)
        self.metrics_dir = str(Path(family_dirs[0]) / "tai42_prometheus")

        spawning.spawn_all(self, manifest_path, family_dirs)
        port_ownership.ensure_own_ports(self)
        # A supervised stack runs its supervisor from the first spawn, like the process
        # manager of a deployment: a worker can be recycled while the fleet boots (a
        # worker joining the bus rolls a recycle over the workers that ran a stored copy of
        # a key its deployment sets). Until boot has converged the supervisor respawns only
        # a graceful exit; a failed exit stays down so readiness surfaces it.
        self.supervisor.start()
        readiness.wait_ready(self)
        self.supervisor.mark_booted()

    def set_port(self, name: str, new_port: int) -> None:
        """Point the stack's port record for ``name`` at ``new_port`` (mirrors :meth:`ports_for`)."""
        if name == "metrics":
            self.metrics_port = new_port
            return
        idx = 0 if name in ("serve", "serve-a", "embed") else 1
        self.app_ports[idx] = new_port

    def teardown(self) -> None:
        if getattr(self, "_torn_down", False):
            return
        self._torn_down = True
        # Stop the respawn supervisor FIRST and wait for any in-flight respawn to finish, so
        # it never re-launches a process this teardown is about to reap (a leak).
        self.supervisor.stop()
        errors: list[str] = []
        errors.extend(self._reap_processes())
        errors.extend(self._release_ports())
        errors.extend(self._release_infra())
        errors.extend(self._reap_relays())

        self.procs.clear()
        if not self.infra.settings.keep_stacks:
            shutil.rmtree(self.root, ignore_errors=True)
        if errors:
            raise RuntimeError("stack teardown found leaks:\n  " + "\n  ".join(errors))

    def _reap_processes(self) -> list[str]:
        """Stop every process group (SIGTERM -> SIGKILL) and assert each was reaped."""
        errors: list[str] = []
        for handle in list(self.procs.values()):
            try:
                handle.terminate()
            except Exception as exc:
                errors.append(f"terminate {handle.name}: {exc!r}")
            if handle.is_running():
                errors.append(f"process {handle.name} still running after SIGKILL (leak)")
        return errors

    def _release_ports(self) -> list[str]:
        """Assert every allocated port was released. A SIGKILL closes the listen socket
        asynchronously (uvicorn workers hold the shared fd and die a beat after the
        master is reaped), so poll briefly before declaring a real leak."""
        errors: list[str] = []
        for port in self.allocated_ports:
            try:
                wait_for(lambda p=port: ports.is_free(p), deadline=5.0, message=f"port {port} never freed")
            except TimeoutError:
                errors.append(f"port {port} still bound after teardown (leak)")
            ports.release_port(port)
        return errors

    def _release_infra(self) -> list[str]:
        """Drop the stack DB, release the Redis index(es), and reap the broker lease
        (a leaked vhost is a teardown error, same as a leaked database)."""
        errors: list[str] = []
        try:
            self.infra.pg.drop_stack_db(self.resources.pg_db)
        except Exception as exc:
            errors.append(f"drop database {self.resources.pg_db}: {exc!r}")
        self.infra.redis.release_db(self.resources.redis_idx)
        if self.resources.checkpoint_redis_idx is not None:
            if self.infra.checkpoint_redis is None:
                errors.append("stack holds a checkpoint Redis DB but infra.checkpoint_redis is None (leak)")
            else:
                self.infra.checkpoint_redis.release_db(self.resources.checkpoint_redis_idx)
        if self.resources.broker_lease is not None:
            try:
                self.resources.broker_lease.release()
            except Exception as exc:
                errors.append(f"release broker vhost {self.resources.broker_lease.vhost}: {exc!r}")
        return errors

    def _reap_relays(self) -> list[str]:
        """Stop every attached relay and assert it leaked no listener/thread — the
        relay is per-stack harness machinery, reaped like a port or a vhost."""
        errors: list[str] = []
        for relay in self._relays:
            try:
                relay.stop()
            except Exception as exc:
                errors.append(f"stop relay: {exc!r}")
            else:
                if relay.is_leaked():
                    errors.append("relay still holds a listener/connection/thread after stop (leak)")
        return errors

    # ---- readiness delegators --------------------------------------------

    async def wait_workers(self, n: int, *, port: int | None = None, deadline: float = 10.0) -> dict[int, str]:
        """Poll the ``e2e_worker_info`` probe until ``n`` distinct worker pids have
        answered, returning each pid mapped to its reported state digest (see
        :func:`tai42_e2e.readiness.wait_workers`)."""
        return await readiness.wait_workers(self, n, port=port, deadline=deadline)

    def wait_fresh_lives(
        self,
        kind: str,
        deadline: float,
        *,
        before: Mapping[str, int] | None = None,
        replaced: Collection[str] | None = None,
        count: int = 1,
    ) -> None:
        readiness.wait_fresh_lives(self, kind, deadline, before=before, replaced=replaced, count=count)

    # ---- client helpers --------------------------------------------------

    @property
    def port_a(self) -> int:
        return self.app_ports[0]

    @property
    def port_b(self) -> int:
        if len(self.app_ports) < 2:
            raise RuntimeError("port_b is only defined for a REPLICAS stack")
        return self.app_ports[1]

    def origin(self, port: int | None = None) -> str:
        """The origin of an app port (default: the primary one)."""
        return f"http://{self.host}:{port or self.port_a}"

    def base_url(self, port: int | None = None) -> str:
        """The URL the tai app is served at on an app port: its origin plus the stack's path prefix."""
        return f"{self.origin(port)}{self.config.path_prefix}"

    def mcp(self, port: int | None = None, path: str | None = None, *, auth: str | None = None) -> McpClient:
        """A client for the MCP endpoint at ``path``, by default the one the stack's transport serves
        (``/sse`` under ``tai serve --transport sse``, else ``/mcp``)."""
        if path is None:
            path = "/sse" if self.config.transport == "sse" else "/mcp"
        read_timeout = mcp_read_timeout(self.config.env, self.infra.settings.boot_timeout)
        return McpClient(f"{self.base_url(port)}{path}", auth=auth, read_timeout=read_timeout)

    def api(self, port: int | None = None) -> ApiClient:
        return ApiClient(self.base_url(port), auth_token=self.auth_token)

    def scrape(self) -> Scrape:
        """Scrape the standalone metrics server (the multiproc reader)."""
        assert self.metrics_port is not None
        return scrape(f"http://{self.host}:{self.metrics_port}/metrics")

    def app_scrape(self, port: int | None = None) -> Scrape:
        """Scrape a serve worker's in-app ``/metrics`` route."""
        return scrape(f"{self.base_url(port)}/metrics")

    def census(self) -> list[BusWorker]:
        """The live fleet currently on the app-owned worker bus — every subscribed
        worker (HTTP ``serve`` workers AND the ``backend`` runtime), scanned off the
        bus presence keys under this stack's namespace. Backend-independent."""
        # Local import: the variants package imports this module, so the census helper
        # is reached at call time to avoid a module-load cycle.
        from tai42_e2e.variants import bus_census

        return bus_census(self.resources.bus_redis_url, self.resources.bus_namespace)

    def records(self, key: str) -> list[str]:
        """The raw JSON strings ``e2e_record`` RPUSH'd under ``key``."""
        return self.infra.redis.records(key)

    def record_keys(self) -> list[str]:
        """Every ``e2e_record`` key currently present — for reading back records keyed
        on a value the test cannot know ahead of the run (a SUT-minted person id)."""
        return self.infra.redis.record_keys()

    def process(self, name: str) -> ProcessHandle:
        return self.procs[name]

    # ---- restart / rotation delegators -----------------------------------

    def restart(self, name: str) -> None:
        """Stop and respawn one process from its saved spec (see :func:`tai42_e2e.restarts.restart`)."""
        restarts.restart(self, name)

    def restart_with_env(self, changes: Mapping[str, str | None]) -> None:
        """Apply env ``changes`` to every serve replica and the backend, then restart them
        (see :func:`tai42_e2e.restarts.restart_with_env`)."""
        restarts.restart_with_env(self, changes)

    def respawn_with_process_env(self, name: str, changes: Mapping[str, str | None]) -> None:
        """Apply env ``changes`` to one supervised process and let the supervisor respawn it
        (see :func:`tai42_e2e.restarts.respawn_with_process_env`)."""
        restarts.respawn_with_process_env(self, name, changes)

    def rotate_connectors_kek(self, *, new_kek: str, previous: list[str]) -> None:
        """Rotate ``CONNECTORS_KEK`` across every serve replica and the backend worker
        (see :func:`tai42_e2e.restarts.rotate_connectors_kek`)."""
        restarts.rotate_connectors_kek(self, new_kek=new_kek, previous=previous)

    def wait_generation_bump(
        self, kind_or_name: str, baseline: int | Mapping[str, int], *, deadline: float = 90.0
    ) -> dict[str, BusWorker]:
        """Wait until a fresh READY life supersedes the baseline generation(s), returning the
        matching census rows keyed by slot name (see :func:`tai42_e2e.restarts.wait_generation_bump`)."""
        return restarts.wait_generation_bump(self, kind_or_name, baseline, deadline=deadline)

    # ---- processes and their ports ---------------------------------------

    def ports_for(self, name: str) -> list[int]:
        """The loopback ports a process kind binds (empty for the backend worker,
        which joins the worker bus over Redis rather than binding a port)."""
        if name.startswith("serve"):
            idx = 0 if name in ("serve", "serve-a") else 1
            return [self.app_ports[idx]]
        if name == "embed":
            return [self.app_ports[0]]
        if name == "metrics" and self.metrics_port is not None:
            return [self.metrics_port]
        return []

    def process_on_port(self, port: int) -> ProcessHandle:
        """The process currently serving the loopback ``port``."""
        with self.supervisor.lock:
            for name, handle in self.procs.items():
                if port in self.ports_for(name):
                    return handle
        raise RuntimeError(f"stack {self.config.name!r}: no process serves port {port}")

    def kill(self, name: str) -> None:
        """SIGKILL one process immediately without respawning (dead-worker
        tests); its presence key lingers until the heartbeat TTL expires."""
        if self.config.supervised:
            # The supervisor would respawn the killed process, defeating the dead-worker
            # scenario — a supervised stack must not be manually killed.
            raise RuntimeError(f"kill({name!r}) is unsupported on a supervised stack; the supervisor would respawn it")
        self.procs[name].kill_now()

    def attach_relay(self, relay: TcpRelay) -> None:
        """Register a per-stack TCP relay for teardown reap + leak-check. The
        infra-outage tests front the stack's Redis/PG with a relay so a test can
        sever the connection mid-run; attaching it here means teardown stops it
        and asserts it left nothing listening (like a leaked port)."""
        self._relays.append(relay)
