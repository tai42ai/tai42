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

The process spawning, child-env construction, and readiness waits live in the
:mod:`~tai42_e2e.spawning`, :mod:`~tai42_e2e.child_env`, and
:mod:`~tai42_e2e.readiness` modules as free functions this class drives; the frozen
config/resource/infra descriptions live in :mod:`~tai42_e2e.topology`."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from tai42_e2e import child_env, ports, readiness, spawning
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.mcp import McpClient, mcp_url
from tai42_e2e.metrics import Scrape, scrape
from tai42_e2e.topology import Infra, StackConfig, StackResources, Topology, _ProcSpec
from tai42_e2e.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tai42_e2e.procs import ProcessHandle
    from tai42_e2e.tcprelay import TcpRelay
    from tai42_e2e.variants import BusWorker

logger = logging.getLogger(__name__)


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
        self._procs: dict[str, ProcessHandle] = {}
        self._specs: dict[str, _ProcSpec] = {}
        self._allocated_ports: list[int] = []
        # Per-stack TCP relays (the infra-outage tests front Redis/PG with one).
        # Attached before boot; stopped and leak-checked in teardown like a port.
        self._relays: list[TcpRelay] = []
        # The main run family's multiproc dir (MULTIWORKER: the shared dir;
        # REPLICAS: replica A's dir, which the backend + metrics join).
        self.metrics_dir: str = ""
        self._config_dir = root / "config"
        self._logs_dir = root / "logs"
        # Respawn-on-exit supervisor (opt-in ``config.supervised``). Serialises every
        # ``_procs``/``_specs`` mutation the supervisor and teardown/restart race on.
        self._supervisor_lock = threading.RLock()
        self._supervisor_stop = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        self._supervised_names: set[str] = set()

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

    def boot(self) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._logs_dir.mkdir(parents=True, exist_ok=True)

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
        self._allocated_ports.extend(self.app_ports)
        if self.config.run_metrics:
            self.metrics_port = ports.allocate_port()
            self._allocated_ports.append(self.metrics_port)

        manifest_path = self._config_dir / "manifest.yml"
        # Seed the manifest verbatim when a raw document is supplied (the
        # comment-preservation scenario), else dump the config dict.
        if self.config.raw_manifest is not None:
            manifest_path.write_text(self.config.raw_manifest, encoding="utf-8")
        else:
            manifest_path.write_text(yaml.safe_dump(self.config.manifest, sort_keys=False), encoding="utf-8")
        child_env.render_env_file(self)

        # One run family per metrics dir. REPLICAS: replica A + backend + metrics
        # share family "a"; replica B is family "b" with no scraper.
        family_dirs = spawning.make_family_dirs(self.root, n_app)
        self.metrics_dir = str(Path(family_dirs[0]) / "tai42_prometheus")

        spawning.spawn_all(self, manifest_path, family_dirs)
        self._ensure_own_ports()
        readiness.wait_ready(self)
        # Only after the fleet is ready: a supervised stack now respawns any serve/backend
        # process that self-exits (a recycle). Before readiness, an early exit is a boot
        # failure surfaced by ``_early_exit_detail``, never a recycle — so the supervisor
        # must not start until boot has converged.
        self._start_supervisor()

    # ---- port-ownership heal ---------------------------------------------

    # How many fresh ports one process may be handed before its bind is declared a
    # genuine failure rather than an ephemeral-port collision.
    _PORT_OWNERSHIP_ATTEMPTS = 6

    def _ensure_own_ports(self) -> None:
        """Confirm every port-binding process listens on the port THIS stack allocated it, healing a collision.

        The allocator probes a free ephemeral port then closes the socket before the
        child binds it, so a foreign process on the shared host can seize that port in the
        gap; the child then fails to bind (``EADDRINUSE``) and exits. A busless stack's
        HTTP-only readiness would pass against the foreign listener and the test would talk
        to the wrong server. For each spawned process that binds a port — the serve fleet,
        the embed host, the metrics server alike — wait until the port is held by this
        process's own session; on the child's bind-failure exit, or a foreign pid holding
        the port, re-allocate a fresh port and respawn, and after the attempts are spent
        raise loudly naming the port and the foreign holder. Runs at the one boot seam, so
        every stack shape is covered without a per-stack copy.
        """
        for name in [n for n in list(self._procs) if self._ports_for(n)]:
            self._heal_process_port(name)

    def _heal_process_port(self, name: str) -> None:
        for _ in range(self._PORT_OWNERSHIP_ATTEMPTS):
            port = self._ports_for(name)[0]
            if self._await_port_binding(name, port) != "collision":
                # "owned": the process holds its own port. "released": the child exited for
                # its OWN reason (not a bind collision) — leave it untouched so readiness
                # surfaces the child's failure unchanged; NEVER respawn a child that died on
                # its own, and log no heal warning.
                return
            logger.warning(
                "stack %r: process %r did not own its port %d (foreign pid(s) %s) — re-allocating and respawning",
                self.config.name,
                name,
                port,
                self._foreign_holder_text(port),
            )
            self._reallocate_and_respawn(name, port)
        port = self._ports_for(name)[0]
        raise RuntimeError(
            f"stack {self.config.name!r}: process {name!r} could not bind an owned port after "
            f"{self._PORT_OWNERSHIP_ATTEMPTS} re-allocations — port {port} held by foreign pid(s) "
            f"{self._foreign_holder_text(port)}"
        )

    @staticmethod
    def _foreign_holder_text(port: int) -> str:
        """The pid(s) holding ``port`` as a plain, comma-joined string for a loud error, or
        ``unknown`` when no diagnostic tool could attribute the listener."""
        pids = ports.listening_pids(port)
        return ", ".join(str(pid) for pid in pids) if pids else "unknown"

    def _await_port_binding(self, name: str, port: int) -> str:
        """Decide the port's fate, distinguishing a bind COLLISION (the heal's to fix) from
        the child's own early exit (readiness's to surface):

        - ``"owned"`` — a listener in this process's session holds the port.
        - ``"collision"`` — a FOREIGN pid holds the port, or the child exited having logged
          the bind error (``EADDRINUSE``); re-allocate and respawn.
        - ``"released"`` — the child exited for its OWN reason with no foreign listener on
          the port (a bad manifest, a missing secret); leave it so readiness raises the
          child's failure unchanged.

        A process that never begins listening within the boot timeout is a genuine bind
        hang and raises through :func:`wait_for`."""
        handle = self._procs[name]

        def decide() -> str | None:
            # Cheap gate first: only reach for the (subprocess-priced) pid attribution once
            # something is actually listening on the port.
            if ports.is_free(port):
                if handle.is_running():
                    return None  # nothing bound yet; keep waiting
                # Exited without ever binding: only a recorded bind failure (a foreign
                # holder since gone) is the heal's; any other exit is the child's own.
                return "collision" if self._exited_on_bind_error(handle) else "released"
            pids = ports.listening_pids(port)
            if pids:
                return "owned" if self._pids_in_session(pids, handle.pid) else "collision"
            # Bound but the diagnostic tools cannot attribute it: ours while our child is
            # alive (a foreign holder would have failed our bind and exited it); a foreign
            # holder once our child has exited.
            return "owned" if handle.is_running() else "collision"

        return wait_for(
            decide,
            deadline=self.infra.settings.boot_timeout,
            message=f"process {name!r} never began listening on port {port}",
        )

    @staticmethod
    def _exited_on_bind_error(handle: ProcessHandle) -> bool:
        """Whether an exited child's output shows it failed to BIND its port (``EADDRINUSE``)
        — the only early exit the port heal owns; every other exit is the child's own boot
        failure, which readiness surfaces unchanged."""
        tail = handle.log_tail().lower()
        return "address already in use" in tail or "[errno 98]" in tail

    @staticmethod
    def _pids_in_session(pids: list[int], leader_pid: int) -> bool:
        """Whether every listener pid belongs to ``leader_pid``'s session — the spawned
        master is a session leader (``start_new_session=True``), so its sid equals its pid
        and its uvicorn worker children inherit that session. A pid that vanished mid-check
        is skipped; confirming none is not ownership."""
        confirmed = False
        for pid in pids:
            try:
                if os.getsid(pid) != leader_pid:
                    return False
            except (ProcessLookupError, PermissionError):
                continue
            confirmed = True
        return confirmed

    def _reallocate_and_respawn(self, name: str, old_port: int) -> None:
        """Reap the child, drop the lost port, allocate a fresh one, rewrite the spec's
        ``--port``, refresh every port-derived env value, and respawn under the same name."""
        self._procs[name].terminate()
        if old_port in self._allocated_ports:
            self._allocated_ports.remove(old_port)
        # The lost port is held by a foreign process, so do NOT assert it frees.
        ports.release_port(old_port)
        new_port = ports.allocate_port()
        self._allocated_ports.append(new_port)
        self._set_port(name, new_port)
        argv = self._specs[name].argv
        for i in range(len(argv) - 1):
            if argv[i] == "--port":
                argv[i + 1] = str(new_port)
                break
        # The port-derived env (origin allowlist, own-origin, replica-B origin — see
        # ``child_env.dynamic_env``) was baked from the OLD port into every spec and the
        # rendered ``.env`` the reload path reads. Re-derive it now so neither this process
        # nor any already-running sibling that inherited a port-derived value advertises or
        # allows the seized port; the freshly-terminated target then respawns with it.
        self._refresh_port_derived_env(target=name)
        spawning.spawn(self, self._specs[name])

    def _refresh_port_derived_env(self, *, target: str) -> None:
        """Re-derive the port-keyed env into every spec and the ``.env``, respawning each
        RUNNING process whose port-derived values changed.

        One seam over ``child_env.dynamic_env`` (no per-profile branch): bus keys do not
        depend on a port so they never trigger a respawn; only a profile that carries a
        port-derived origin (all-ports allowlist, own-origin, replica-B origin) has a
        process to refresh. The ``target`` was reaped by the caller and is respawned by it
        with the updated spec, so it is not respawned here even if still winding down.
        """
        fresh = child_env.dynamic_env(self)
        child_env.render_env_file(self)
        for pname, spec in list(self._specs.items()):
            if all(spec.env.get(key) == value for key, value in fresh.items()):
                continue
            spec.env.update(fresh)
            if pname != target and self._procs[pname].is_running():
                self._procs[pname].terminate()
                spawning.spawn(self, spec)

    def _set_port(self, name: str, new_port: int) -> None:
        """Point the stack's port record for ``name`` at ``new_port`` (mirrors :meth:`_ports_for`)."""
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
        self._stop_supervisor()
        errors: list[str] = []
        errors.extend(self._reap_processes())
        errors.extend(self._release_ports())
        errors.extend(self._release_infra())
        errors.extend(self._reap_relays())

        self._procs.clear()
        if not self.infra.settings.keep_stacks:
            shutil.rmtree(self.root, ignore_errors=True)
        if errors:
            raise RuntimeError("stack teardown found leaks:\n  " + "\n  ".join(errors))

    def _reap_processes(self) -> list[str]:
        """Stop every process group (SIGTERM -> SIGKILL) and assert each was reaped."""
        errors: list[str] = []
        for handle in list(self._procs.values()):
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
        for port in self._allocated_ports:
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

    def _wait_backend_census(self, deadline: float, baseline: Mapping[str, int] | None = None) -> None:
        readiness.wait_backend_census(self, deadline, baseline)

    # ---- client helpers --------------------------------------------------

    @property
    def port_a(self) -> int:
        return self.app_ports[0]

    @property
    def port_b(self) -> int:
        if len(self.app_ports) < 2:
            raise RuntimeError("port_b is only defined for a REPLICAS stack")
        return self.app_ports[1]

    def mcp(self, port: int | None = None, path: str = "/mcp", *, auth: str | None = None) -> McpClient:
        return McpClient(mcp_url(self.host, port or self.port_a, path), auth=auth)

    def api(self, port: int | None = None) -> ApiClient:
        return ApiClient(f"http://{self.host}:{port or self.port_a}", auth_token=self.auth_token)

    def scrape(self) -> Scrape:
        """Scrape the standalone metrics server (the multiproc reader)."""
        assert self.metrics_port is not None
        return scrape(f"http://{self.host}:{self.metrics_port}/metrics")

    def app_scrape(self, port: int | None = None) -> Scrape:
        """Scrape a serve worker's in-app ``/metrics`` route."""
        return scrape(f"http://{self.host}:{port or self.port_a}/metrics")

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
        return self._procs[name]

    # ---- restart / rotation ----------------------------------------------

    def restart(self, name: str) -> None:
        """Stop and respawn one process from its saved spec (component-restart
        tests). The new process re-enters readiness for its own port kind."""
        if self.config.supervised:
            # The respawn-on-exit supervisor would observe the terminated handle and
            # double-spawn a second replacement — a supervised stack recycles through the
            # supervisor (a graceful self-exit), never a manual restart.
            raise RuntimeError(f"restart({name!r}) is unsupported on a supervised stack; recycle via the supervisor")
        handle = self._procs[name]
        # The backend worker joins no port — its readiness keys on a fresh ``backend``-kind
        # census LIFE. A restarted worker reuses its stable slot name at an incremented
        # generation, so capture the pre-restart backend lives (``{name: generation}``)
        # BEFORE the kill; the post-restart wait then holds until those lives are gone and
        # a fresh READY backend life has joined, keying on the generation not a new string.
        before_backends = (
            {w.name: w.generation for w in self.census() if w.kind == "backend"} if name == "backend" else {}
        )
        handle.terminate()
        # The respawn reuses the same port; wait for the killed process to release
        # it before rebinding, else the new process fails to bind and exits early.
        for port in self._ports_for(name):
            wait_for(lambda p=port: ports.is_free(p), deadline=5.0, message=f"port {port} never freed before restart")
        spec = self._specs[name]
        spawning.spawn(self, spec)
        self._wait_after_restart(name, before_backends)

    def rotate_connectors_kek(self, *, new_kek: str, previous: list[str]) -> None:
        """Rotate ``CONNECTORS_KEK`` across every serve replica and the backend worker.

        Sets the new current key and the previous-key ring in each running process's env,
        then restarts it so the new keys take effect — modelling an operator rotating the
        deployment's KEK. An empty ``previous`` clears ``CONNECTORS_KEK_PREVIOUS`` (the old
        key retired after the re-encrypt sweep converged)."""
        # The app reads the KEK from the rendered ``.env`` file (TaiBaseSettings' env_file),
        # so the shared ``config.env`` — the .env source — must carry the rotated keys too;
        # updating only each process env leaves a restarted replica booting the stale .env.
        self.config.env["CONNECTORS_KEK"] = new_kek
        if previous:
            self.config.env["CONNECTORS_KEK_PREVIOUS"] = ",".join(previous)
        else:
            self.config.env.pop("CONNECTORS_KEK_PREVIOUS", None)
        child_env.render_env_file(self)
        for name in [n for n in self._specs if n.startswith("serve") or n == "backend"]:
            env = self._specs[name].env
            env["CONNECTORS_KEK"] = new_kek
            if previous:
                env["CONNECTORS_KEK_PREVIOUS"] = ",".join(previous)
            else:
                env.pop("CONNECTORS_KEK_PREVIOUS", None)
            self.restart(name)

    def _ports_for(self, name: str) -> list[int]:
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

    def kill(self, name: str) -> None:
        """SIGKILL one process immediately without respawning (dead-worker
        tests); its presence key lingers until the heartbeat TTL expires."""
        if self.config.supervised:
            # The supervisor would respawn the killed process, defeating the dead-worker
            # scenario — a supervised stack must not be manually killed.
            raise RuntimeError(f"kill({name!r}) is unsupported on a supervised stack; the supervisor would respawn it")
        self._procs[name].kill_now()

    def attach_relay(self, relay: TcpRelay) -> None:
        """Register a per-stack TCP relay for teardown reap + leak-check. The
        infra-outage tests front the stack's Redis/PG with a relay so a test can
        sever the connection mid-run; attaching it here means teardown stops it
        and asserts it left nothing listening (like a leaked port)."""
        self._relays.append(relay)

    def _wait_after_restart(self, name: str, before_backends: Mapping[str, int] | None = None) -> None:
        deadline = self.infra.settings.boot_timeout
        if name.startswith("serve"):
            idx = 0 if name in ("serve", "serve-a") else 1
            port = self.app_ports[idx]
            readiness.wait_http_ok(self, f"http://{self.host}:{port}/health", deadline, "app health")
            # A respawned serve worker re-runs its boot self-resync gate on rejoin;
            # drain it (where the profile carries the probe) so a test acting right
            # after the restart does not race the gate, exactly as at boot.
            if child_env.needs_bus(self.config):
                readiness.run_readiness_coro(readiness.drain_gate_coro(self, [port], deadline))
        elif name == "embed":
            readiness.wait_http_ok(self, f"http://{self.host}:{self.app_ports[0]}/health", deadline, "app health")
            if child_env.needs_bus(self.config):
                readiness.run_readiness_coro(readiness.drain_gate_coro(self, [self.app_ports[0]], deadline))
        elif name == "metrics":
            assert self.metrics_port is not None
            readiness.wait_http_ok(self, f"http://{self.host}:{self.metrics_port}/metrics", deadline, "metrics")
        elif name == "backend":
            self._wait_backend_census(deadline, baseline=before_backends)

    # ---- respawn-on-exit supervisor (opt-in supervised shape) ------------

    def _start_supervisor(self) -> None:
        """Start the respawn-on-exit supervisor once the fleet is ready. Only the
        recyclable kinds (serve + backend) are watched — metrics is not a recycle
        target and joins no bus census."""
        if not self.config.supervised:
            return
        with self._supervisor_lock:
            self._supervised_names = {name for name in self._procs if name.startswith("serve") or name == "backend"}
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop, name=f"tai-e2e-supervisor-{self.config.name}", daemon=True
        )
        self._supervisor_thread.start()

    def _stop_supervisor(self) -> None:
        """Stop the supervisor and wait for any in-flight respawn to finish, so teardown
        never races a re-launch. Idempotent."""
        self._supervisor_stop.set()
        thread = self._supervisor_thread
        if thread is not None:
            thread.join(timeout=30.0)
            self._supervisor_thread = None

    def _supervisor_loop(self) -> None:
        while not self._supervisor_stop.wait(0.2):
            with self._supervisor_lock:
                if self._supervisor_stop.is_set():
                    return
                for name in list(self._supervised_names):
                    handle = self._procs.get(name)
                    if handle is not None and handle.poll() is not None:
                        self._respawn_supervised(name)

    def _respawn_supervised(self, name: str) -> None:
        """Re-launch a self-exited supervised process from its saved spec — the external
        supervisor a graceful recycle self-exit assumes. The caller holds
        ``_supervisor_lock``. Reaps the exited handle, waits for its port to free, then
        respawns; the replacement rejoins the bus census under its stable slot name at an
        incremented generation (a new pid)."""
        old = self._procs.get(name)
        if old is not None:
            with contextlib.suppress(Exception):
                old.terminate()
        for port in self._ports_for(name):
            with contextlib.suppress(TimeoutError):
                wait_for(lambda p=port: ports.is_free(p), deadline=15.0, message=f"port {port} never freed for respawn")
        spawning.spawn(self, self._specs[name])

    def wait_generation_bump(
        self, kind_or_name: str, baseline: int | Mapping[str, int], *, deadline: float = 90.0
    ) -> dict[str, BusWorker]:
        """Wait until a fresh READY life supersedes the baseline generation(s), returning
        the matching census rows keyed by slot name.

        A worker's slot name is STABLE across lives; a new life bumps its generation. Two
        forms distinguished by ``baseline``:

        * Single-life (``baseline`` an ``int``): ``kind_or_name`` is a slot NAME. Waits
          until that name shows a READY row at a generation greater than ``baseline`` — the
          scenario-scoped deterministic wait for one named slot's next life.
        * Fleet (``baseline`` a ``{name: generation}`` map): ``kind_or_name`` is a worker
          KIND. Waits until EVERY baseline name shows a READY row of that kind at a
          generation greater than its baseline — proves a recycle/respawn rolled every
          targeted life without keying on a fresh unrelated id."""
        if isinstance(baseline, int):
            name, base_gen = kind_or_name, baseline

            def one_probe() -> bool:
                early = self._early_exit_detail_supervised()
                if early is not None:
                    raise RuntimeError(f"generation-bump wait: {early}")
                row = next((w for w in self.census() if w.name == name), None)
                return row is not None and row.state == "ready" and row.generation > base_gen

            wait_for(
                one_probe, deadline=deadline, message=f"{name!r} never reached a ready life past generation {base_gen}"
            )
            row = next(w for w in self.census() if w.name == name)
            return {name: row}

        kind, targets = kind_or_name, dict(baseline)

        def fleet_probe() -> bool:
            early = self._early_exit_detail_supervised()
            if early is not None:
                raise RuntimeError(f"generation-bump wait: {early}")
            rows = {w.name: w for w in self.census() if w.kind == kind}
            return all(
                name in rows and rows[name].state == "ready" and rows[name].generation > gen
                for name, gen in targets.items()
            )

        wait_for(
            fleet_probe,
            deadline=deadline,
            message=f"not every {kind!r} life bumped past its baseline generation ({targets})",
        )
        rows = {w.name: w for w in self.census() if w.kind == kind}
        return {name: rows[name] for name in targets if name in rows}

    def _early_exit_detail_supervised(self) -> str | None:
        """Like ``readiness.early_exit_detail`` but tolerant of the supervised churn: a
        serve/backend handle momentarily exited is being respawned by the supervisor, not a
        failure. Only a NON-supervised process that exited (or a supervised one with no
        respawn thread) is a real early exit worth surfacing."""
        with self._supervisor_lock:
            for handle in self._procs.values():
                if handle.name in self._supervised_names:
                    continue
                if not handle.is_running():
                    return f"process {handle.name!r} exited early (code {handle.poll()}):\n{handle.log_tail()}"
        return None
