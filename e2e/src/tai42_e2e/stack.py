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
The harness asserts it never sets it (see :meth:`_child_env`)."""

from __future__ import annotations

import asyncio
import contextlib
import enum
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import yaml

from tai42_e2e import ports
from tai42_e2e.httpapi import ApiClient, _is_reloading
from tai42_e2e.mcp import McpClient, mcp_url
from tai42_e2e.metrics import Scrape, scrape
from tai42_e2e.pg import PostgresAdmin
from tai42_e2e.procs import ProcessHandle
from tai42_e2e.redisx import RedisAdmin
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for, wait_for_async

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from tai42_e2e.tcprelay import TcpRelay
    from tai42_e2e.variants import BrokerLease, BusOrigin, Variants


def tai_bin() -> str:
    """The ``tai`` console script from the active venv — the real entrypoint
    production runs, never ``python -c``."""
    candidate = Path(sys.executable).parent / "tai"
    if not candidate.exists():
        raise RuntimeError(f"tai console script not found next to interpreter at {candidate}")
    return str(candidate)


def uvicorn_bin() -> str:
    """The ``uvicorn`` console script from the active venv — the server a user runs
    to serve their own embed host app. Ships in the skeleton dep tree."""
    candidate = Path(sys.executable).parent / "uvicorn"
    if not candidate.exists():
        raise RuntimeError(f"uvicorn console script not found next to interpreter at {candidate}")
    return str(candidate)


def spawn_expect_refusal(argv: list[str], env: dict[str, str], cwd: str | Path, *, timeout: float = 20.0) -> str:
    """Spawn a process DIRECTLY (outside the stack readiness framework) and require
    it to REFUSE to boot — a nonzero exit within ``timeout`` — returning its stderr
    so the boot-rules scenarios can assert the refused setting is named on it.

    The boot-refusal scenarios drive ``tai serve``/``tai backend`` and a
    factory-string ``uvicorn`` against a bus-requiring config with no
    ``TAI_BUS_REDIS_URL``: the process must exit nonzero, naming the setting. A
    process that SERVES instead of refusing never exits — it (and its worker process
    group) is killed and this raises loudly, since a boot that should have been
    refused is itself the failure."""
    import os
    import signal
    import subprocess

    proc = subprocess.Popen(
        argv,
        env=env,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # It served instead of refusing: reap the whole process group (a serve
        # master forks worker children) and fail loudly.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5.0)
        raise RuntimeError(f"process did not refuse to boot within {timeout}s (it served): {argv!r}") from exc
    if proc.returncode == 0:
        raise RuntimeError(f"process was expected to refuse to boot but exited 0: {argv!r}\nstderr:\n{stderr}")
    return stderr


async def _probe_tolerating_reloading[T](open_and_call: Callable[[], Awaitable[T]]) -> T | None:
    """Await a boot/restart wait probe that opens an MCP client, returning ``None`` when
    it raises the reload gate's retriable ``reloading`` envelope so the enclosing wait
    keeps polling within its own deadline; any other error propagates loudly.

    A worker still holding its boot/reload self-resync gate rejects the MCP initialize
    handshake with that envelope (a ``503`` the authenticated request path answers while
    the identity registry is mid-rebuild). The fastmcp client raises ``HTTPStatusError``
    from ``__aenter__`` — before a session exists — so the tool-call ``retry_on_reloading``
    path never sees it, and the reloading-vs-real decision is made here on the raised
    response through the one canonical :func:`_is_reloading` check. ``open_and_call`` must
    never itself return ``None``, so a ``None`` result unambiguously means "still reloading"."""
    try:
        return await open_and_call()
    except httpx.HTTPStatusError as exc:
        if _is_reloading(exc.response):
            return None
        raise


class InfraUnavailable(RuntimeError):
    """The shared infra (Redis / Postgres / a backend's broker) could not be
    reached, or a variant selection is unknown; carries the compose hint. Raised
    at session start so a misconfiguration fails loudly, never cryptically
    mid-suite."""


class Topology(enum.Enum):
    """How the ``tai serve`` fleet is shaped.

    ``MULTIWORKER`` is one master with ``--workers N`` on one port (shared
    run-id + mmap dir) — the model the metrics round-trip and import-order
    probes need. ``REPLICAS`` is two ``--workers 1`` masters on two ports
    (shared config/Redis/PG, per-replica metrics dirs) — deterministic A/B
    addressing for every cross-worker and Redis-contention test."""

    MULTIWORKER = "multiworker"
    REPLICAS = "replicas"


@dataclass(frozen=True)
class Infra:
    """Shared, session-scoped services and their admin clients, plus the one
    variant set this process runs under (resolved once at ``connect_infra``)."""

    settings: HarnessSettings
    redis: RedisAdmin
    pg: PostgresAdmin
    variants: Variants
    # The module-capable checkpoint Redis admin (RediSearch + RedisJSON), present
    # only when ``TAI_E2E_CHECKPOINT_REDIS_URL`` is set. The langgraph redis
    # checkpoint/store agents leg allocates its own logical DB here; ``None`` means
    # that leg's stacks skip.
    checkpoint_redis: RedisAdmin | None = None


@dataclass(frozen=True)
class StackResources:
    """The per-stack coordinates a manifest/env builder needs: the allocated
    Redis logical DB, the Postgres database name, the on-disk storage root, and
    optional harness-server URLs the SUT points at."""

    redis_idx: int
    redis_url: str
    probe_redis_url: str
    pg_host: str
    pg_port: int
    pg_user: str
    pg_password: str
    pg_db: str
    storage_root: str
    # The app-owned worker bus coordinates. The bus Redis is reached through its
    # OWN endpoint (independent of ``redis_url``, so a bus-outage scenario can sever
    # the bus without severing auth/feature stores), and the namespace is unique
    # per stack (bus pub/sub channels + presence keys are server-global — the
    # per-stack logical-db isolation does NOT isolate them). Always filled by
    # ``allocate_resources``; the empty defaults exist only for the manifest-render
    # sentinels that never boot a bus.
    bus_redis_url: str = ""
    bus_namespace: str = ""
    # The per-stack broker: the celery variant's isolated vhost AMQP URL and the
    # lease that reaps it in teardown. ``None`` for backends that ride on Redis.
    broker_url: str | None = None
    broker_lease: BrokerLease | None = None
    # The per-stack logical DB on the module-capable checkpoint Redis (the
    # langgraph redis checkpoint/store provider's home). ``None`` on every stack
    # that runs the in-process ``memory`` provider instead.
    checkpoint_redis_idx: int | None = None
    checkpoint_redis_url: str | None = None
    llm_base_url: str | None = None
    gh_webhook_secret: str | None = None
    # The Stripe payments profile's three coordinates. ``stripe_webhook_secret`` is the
    # HMAC secret the topic's ``stripe`` verifier reads and the test signs deliveries with
    # (held test-side, so it cannot be minted inside the manifest builder the way a
    # channel verify-token is). ``bridge_callback_secret`` is the one value BOTH the
    # callback door's ``shared_secret`` verifier and the bridge tool read. ``stripe_stub_base``
    # is the in-process ``FakeStripe`` origin the tools' ``STRIPE_API_BASE`` points at — a
    # resource because the stub's port is allocated at fixture time and the builder only
    # reads it (the two-fixture wiring ``channel_stack`` uses for its provider stubs).
    stripe_webhook_secret: str | None = None
    bridge_callback_secret: str | None = None
    stripe_stub_base: str | None = None
    # The built Studio dist the skeleton serves (STUDIO_DIST_PATH) for the
    # browser-e2e profile.
    studio_dist_path: str | None = None
    connectors_kek: str | None = None
    connectors_state_hmac_key: str | None = None
    idp_base_url: str | None = None
    # The in-process signing OIDC issuer's origin (the extended ``OAuthIdp``'s
    # ``base_url``) the oidc stack points its accounts-oidc / identity-oidc issuer
    # config at. ``None`` on every stack that runs no OIDC provider.
    oidc_issuer_base_url: str | None = None
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    # The in-process channel-provider stub origins the channel profile points the
    # plugins' outbound API base URLs at (``CHANNEL_<X>_API_BASE_URL``). ``None``
    # on every non-channel stack.
    telegram_api_base_url: str | None = None
    slack_api_base_url: str | None = None
    twilio_api_base_url: str | None = None
    whatsapp_api_base_url: str | None = None
    # The harness-run marketplace registry's public base URL the skeleton's
    # marketplace client points at (``MARKETPLACE_URL``), and the fixture package
    # index's origin the installer resolves wheels from (``PIP_INDEX_URL`` is
    # ``{package_index_url}/simple/``). ``None`` on every non-marketplace stack.
    marketplace_url: str | None = None
    package_index_url: str | None = None


@dataclass(frozen=True)
class StackConfig:
    """A frozen description of one stack: its shape, the rendered manifest, the
    feature env map, and which optional processes to run. Profiles in
    :mod:`tai42_e2e.manifests` are named presets of this."""

    name: str
    topology: Topology
    manifest: dict
    env: dict[str, str]
    # A verbatim manifest document seeded to disk BYTE-for-byte instead of
    # ``yaml.safe_dump(manifest)`` — the comment-preservation scenario seeds a
    # ruamel-normalized commented manifest this way (``safe_dump`` cannot carry
    # comments). ``None`` uses the dict-dump path.
    raw_manifest: str | None = None
    workers: int = 2
    run_backend: bool = True
    run_metrics: bool = True
    auth: bool = False
    # Serve the SUT as a user-owned embed host (uvicorn running the host FastAPI
    # app in ``tai42_e2e_fixtures.embed_main`` that mounts ``create_app()``) instead
    # of the ``tai serve`` fleet. One app process, in-process metrics mode; a
    # backend worker still joins the app-owned worker bus.
    embed: bool = False
    # Per-process CWD overrides, keyed by process name (the shared-dir test launches
    # the three kinds from three different working directories).
    cwd_overrides: dict[str, str] = field(default_factory=dict)
    # Env keys that must carry this stack's own loopback app origins
    # (``http://host:port`` for every app port, comma-joined). The origins are
    # only known after boot allocates the ports, so a profile names the keys and
    # the stack fills them in at boot — e.g. the connectors profile pins
    # ``CONNECTORS_REDIRECT_URI_ALLOWLIST`` to the origins the OAuth connect flow
    # signs from ``request.base_url``.
    origin_allowlist_env_keys: list[str] = field(default_factory=list)
    # Env keys that must carry the SINGLE replica-B loopback origin
    # (``http://host:port_b``), only known after boot allocates the ports — the
    # channel profile pins ``INTERACTIONS_PUBLIC_BASE_URL`` (so an ask minted on
    # replica A is answered through the callback door on replica B) and the
    # telegram ``CHANNEL_TELEGRAM_PUBLIC_BASE_URL`` (the setWebhook URL) at B.
    # Only meaningful on a REPLICAS stack (two app ports).
    replica_b_origin_env_keys: list[str] = field(default_factory=list)


@dataclass
class _ProcSpec:
    """Everything needed to (re)spawn one process — kept so ``restart`` can
    rebuild an identical handle."""

    name: str
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    log_path: Path


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
        self._render_env_file()

        # One run family per metrics dir. REPLICAS: replica A + backend + metrics
        # share family "a"; replica B is family "b" with no scraper.
        family_dirs = self._make_family_dirs(n_app)
        self.metrics_dir = str(Path(family_dirs[0]) / "tai42_prometheus")

        self._spawn_all(manifest_path, family_dirs)
        self._wait_ready()

    def teardown(self) -> None:
        if getattr(self, "_torn_down", False):
            return
        self._torn_down = True
        errors: list[str] = []
        # Stop every process group (SIGTERM -> SIGKILL) and assert reaped.
        for handle in list(self._procs.values()):
            try:
                handle.terminate()
            except Exception as exc:
                errors.append(f"terminate {handle.name}: {exc!r}")
            if handle.is_running():
                errors.append(f"process {handle.name} still running after SIGKILL (leak)")
        # Assert every port was released. A SIGKILL closes the listen socket
        # asynchronously (uvicorn workers hold the shared fd and die a beat after
        # the master is reaped), so poll briefly before declaring a real leak.
        for port in self._allocated_ports:
            try:
                wait_for(lambda p=port: ports.is_free(p), deadline=5.0, message=f"port {port} never freed")
            except TimeoutError:
                errors.append(f"port {port} still bound after teardown (leak)")
            ports.release_port(port)
        # Drop the stack DB, release the Redis index, and reap the broker lease
        # (a leaked vhost is a teardown error, same as a leaked database).
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
        # Stop every attached relay and assert it leaked no listener/thread — the
        # relay is per-stack harness machinery, reaped like a port or a vhost.
        for relay in self._relays:
            try:
                relay.stop()
            except Exception as exc:
                errors.append(f"stop relay: {exc!r}")
            else:
                if relay.is_leaked():
                    errors.append("relay still holds a listener/connection/thread after stop (leak)")

        self._procs.clear()
        if not self.infra.settings.keep_stacks:
            shutil.rmtree(self.root, ignore_errors=True)
        if errors:
            raise RuntimeError("stack teardown found leaks:\n  " + "\n  ".join(errors))

    # ---- spawning --------------------------------------------------------

    def _tai_bin(self) -> str:
        return tai_bin()

    def _uvicorn_bin(self) -> str:
        return uvicorn_bin()

    def _make_family_dirs(self, n_app: int) -> list[str]:
        dirs: list[str] = []
        for i in range(n_app):
            family = self.root / f"tmp-{chr(ord('a') + i)}"
            (family / "tai42_prometheus").mkdir(parents=True, exist_ok=True)
            dirs.append(str(family))
        return dirs

    def _child_env(self, tmpdir: str, cwd_override: str | None) -> dict[str, str]:
        """A clean child env built from scratch: PATH/HOME/venv-bin, the stack's feature
        env, and TMPDIR for the run-family metrics dir. Never an ``os.environ`` passthrough,
        and never ``PROMETHEUS_MULTIPROC_DIR`` (the entrypoint stamps that itself)."""
        import os

        venv_bin = str(Path(sys.executable).parent)
        env: dict[str, str] = {
            "PATH": os.pathsep.join([venv_bin, "/usr/local/bin", "/usr/bin", "/bin"]),
            "HOME": os.environ.get("HOME", str(self.root)),
            "TMPDIR": tmpdir,
            "TAI_CONFIG_MODE": "file",
            "TAI_CONFIG_DIR_PATH": str(self._config_dir),
            "TAI_MANIFEST_PATH": str(self._config_dir / "manifest.yml"),
        }
        env.update(self.config.env)
        env.update(self._origin_allowlist_env())
        env.update(self._replica_b_origin_env())
        # The worker bus env lands LAST so its mandatory URL + namespace win; the bus
        # TIMING knobs are pinned through config.env, which this never touches.
        env.update(self._bus_env())
        if "PROMETHEUS_MULTIPROC_DIR" in env:
            raise RuntimeError(
                "the harness must never set PROMETHEUS_MULTIPROC_DIR in a child env; "
                "the metrics dir is controlled via TMPDIR so the entrypoint stamps it (C2)"
            )
        # A per-process CWD override still needs load_dotenv to find .env, so the env
        # carries the config dir explicitly.
        _ = cwd_override
        return env

    def _spawn(self, spec: _ProcSpec) -> None:
        handle = ProcessHandle(name=spec.name, argv=spec.argv, cwd=spec.cwd, env=spec.env, log_path=spec.log_path)
        self._specs[spec.name] = spec
        self._procs[spec.name] = handle
        handle.start()

    def _spawn_all(self, manifest_path: Path, family_dirs: list[str]) -> None:
        tai = self._tai_bin()
        if self.config.embed:
            self._spawn_embed_host(family_dirs[0])
        else:
            self._spawn_serve_fleet(tai, manifest_path, family_dirs)

        # The backend worker + metrics server join run-family "a".
        if self.config.run_backend:
            name = "backend"
            cwd_override = self.config.cwd_overrides.get(name)
            cwd = Path(cwd_override) if cwd_override else self._config_dir
            self._spawn(
                _ProcSpec(
                    name=name,
                    argv=[tai, "backend", "worker", "--manifest-path", str(manifest_path)],
                    cwd=cwd,
                    env=self._child_env(family_dirs[0], cwd_override),
                    log_path=self._logs_dir / "backend.log",
                )
            )
            # Extra backend processes the variant requires alongside the worker
            # (celery's RedBeat / rq's rq-scheduler; arq needs none). Each is a
            # full ``tai backend <args>`` process with its own ProcessHandle, log,
            # and teardown leak-reap — an early exit aborts boot loudly through the
            # shared ``_early_exit_detail`` readiness check, exactly like the worker.
            for extra_args in self.infra.variants.backend.extra_backend_processes():
                extra_name = f"backend-{extra_args[0]}"
                extra_cwd = self.config.cwd_overrides.get(extra_name)
                self._spawn(
                    _ProcSpec(
                        name=extra_name,
                        argv=[tai, "backend", *extra_args, "--manifest-path", str(manifest_path)],
                        cwd=Path(extra_cwd) if extra_cwd else self._config_dir,
                        env=self._child_env(family_dirs[0], extra_cwd),
                        log_path=self._logs_dir / f"{extra_name}.log",
                    )
                )
        if self.config.run_metrics:
            assert self.metrics_port is not None
            name = "metrics"
            cwd_override = self.config.cwd_overrides.get(name)
            cwd = Path(cwd_override) if cwd_override else self._config_dir
            self._spawn(
                _ProcSpec(
                    name=name,
                    argv=[tai, "metrics", "--host", self.host, "--port", str(self.metrics_port)],
                    cwd=cwd,
                    env=self._child_env(family_dirs[0], cwd_override),
                    log_path=self._logs_dir / "metrics.log",
                )
            )

    def _spawn_serve_fleet(self, tai: str, manifest_path: Path, family_dirs: list[str]) -> None:
        """Spawn the ``tai serve`` fleet — one master per app port, honouring the
        stack's topology (MULTIWORKER: ``--workers N`` on one port; REPLICAS: two
        one-worker masters on two ports)."""
        n_app = len(self.app_ports)
        for i, port in enumerate(self.app_ports):
            name = "serve" if n_app == 1 else f"serve-{chr(ord('a') + i)}"
            workers = self.config.workers if self.config.topology is Topology.MULTIWORKER else 1
            argv = [
                tai,
                "serve",
                "--host",
                self.host,
                "--port",
                str(port),
                "--workers",
                str(workers),
                "--manifest-path",
                str(manifest_path),
            ]
            # Multiple workers on the stateful http transport pin each MCP session
            # to the worker that created it, which the skeleton refuses to start;
            # stateless http is exactly what a MULTIWORKER stack wants (requests
            # spread across workers so cross-worker seams and the metrics
            # round-trip are exercised).
            if workers > 1:
                argv.append("--stateless-http")
            cwd_override = self.config.cwd_overrides.get(name)
            cwd = Path(cwd_override) if cwd_override else self._config_dir
            self._spawn(
                _ProcSpec(
                    name=name,
                    argv=argv,
                    cwd=cwd,
                    env=self._child_env(family_dirs[i], cwd_override),
                    log_path=self._logs_dir / f"{name}.log",
                )
            )

    def _spawn_embed_host(self, family_dir: str) -> None:
        """Spawn the user-owned embed host: ``uvicorn`` serving the host FastAPI
        app in ``tai42_e2e_fixtures.embed_main`` that mounts ``create_app()``. One process
        on the single app port; the clean child env carries no ``PROMETHEUS_MULTIPROC_DIR``,
        so the mounted app comes up in in-process metrics mode — the surface the embed
        suite scrapes."""
        name = "embed"
        port = self.app_ports[0]
        cwd_override = self.config.cwd_overrides.get(name)
        cwd = Path(cwd_override) if cwd_override else self._config_dir
        argv = [
            self._uvicorn_bin(),
            "tai42_e2e_fixtures.embed_main:app",
            "--host",
            self.host,
            "--port",
            str(port),
        ]
        self._spawn(
            _ProcSpec(
                name=name,
                argv=argv,
                cwd=cwd,
                env=self._child_env(family_dir, cwd_override),
                log_path=self._logs_dir / f"{name}.log",
            )
        )

    # ---- readiness -------------------------------------------------------

    def _wait_ready(self) -> None:
        deadline = self.infra.settings.boot_timeout
        for port in self.app_ports:
            self._wait_http_ok(f"http://{self.host}:{port}/health", deadline, "app health")
        if self.config.run_metrics:
            assert self.metrics_port is not None
            self._wait_http_ok(f"http://{self.host}:{self.metrics_port}/metrics", deadline, "metrics")
        self._wait_fleet_converged(deadline)

    def _wait_fleet_converged(self, deadline: float) -> None:
        """Block until the whole expected fleet is on the bus AND every serve worker
        has left its boot-time self-resync reload gate, so a test acting the instant
        the stack fixture returns never races an incomplete fleet or a still-held gate.

        A busless single-worker stack joins no bus — no presence keys, no on-ready
        self-resync — so HTTP health is its full readiness and this returns at once."""
        if not self._needs_bus():
            return
        self._wait_full_census(deadline)
        self._drain_boot_gate(deadline)

    def _expected_serve_origins(self) -> int:
        """How many ``serve``-kind presence origins the booted fleet registers: a
        REPLICAS stack runs one worker per app port, a MULTIWORKER master runs
        ``--workers N`` on its single port, and the embed host is one app process
        (a MULTIWORKER shape with ``workers=1``)."""
        if self.config.topology is Topology.REPLICAS:
            return len(self.app_ports)
        return self.config.workers

    def _serve_workers_on_port(self) -> int:
        """The serve-worker count behind a single app port — the whole MULTIWORKER
        ``--workers N`` master, or the lone worker of one REPLICAS master."""
        if self.config.topology is Topology.REPLICAS:
            return 1
        return self.config.workers

    def _wait_full_census(self, deadline: float) -> None:
        """Poll the bus census until the FULL expected fleet is present — every
        serve-origin the topology spawns plus the backend origin when a backend is
        registered — so readiness never returns on a half-formed fleet."""
        expected_serve = self._expected_serve_origins()

        def probe() -> bool:
            early = self._early_exit_detail()
            if early is not None:
                raise RuntimeError(f"fleet census: {early}")
            origins = self.census()
            if sum(1 for o in origins if o.kind == "serve") < expected_serve:
                return False
            return not (self.config.run_backend and not any(o.kind == "backend" for o in origins))

        want = f"{expected_serve} serve origins" + (" + backend" if self.config.run_backend else "")
        wait_for(probe, deadline=deadline, message=f"the worker-bus census never reached the full fleet ({want})")

    def _drain_boot_gate(self, deadline: float) -> None:
        """Drive a gated probe on every app port until it answers non-``reloading``,
        so the boot-time self-resync gate has cleared fleet-wide before the fixture
        returns. A single-port MULTIWORKER master spreads stateless requests across its
        workers, so the probe runs until every worker pid has answered clear; a
        REPLICAS port owns one worker, so one clear answer per port suffices."""
        self._run_readiness_coro(self._drain_gate_coro(self.app_ports, deadline))

    async def _drain_gate_coro(self, app_ports: list[int], deadline: float) -> None:
        # A profile that carries no ``e2e_worker_info`` probe never fires an immediate
        # gated tool call in its tests, so its gate needs no draining here — the
        # full-census wait is that profile's convergence.
        async with self.mcp(auth=self.auth_token) as client:
            if "e2e_worker_info" not in await client.tool_names():
                return
        # Positive confirmation the boot self-resync gate has cleared: each serve worker
        # answers a real ``e2e_worker_info`` call non-``reloading`` before the fixture
        # returns, so a test firing a gated request the instant the stack is ready never
        # races the gate.
        workers = self._serve_workers_on_port()
        for port in app_ports:
            await self.wait_workers(workers, port=port, deadline=deadline)

    def _early_exit_detail(self) -> str | None:
        for handle in self._procs.values():
            if not handle.is_running():
                return f"process {handle.name!r} exited early (code {handle.poll()}):\n{handle.log_tail()}"
        return None

    def _wait_http_ok(self, url: str, deadline: float, label: str) -> None:
        def probe() -> bool:
            early = self._early_exit_detail()
            if early is not None:
                raise RuntimeError(f"{label}: {early}")
            try:
                # 503 while a worker warms is "not ready", not a failure.
                return httpx.get(url, timeout=2.0).status_code == 200
            except httpx.HTTPError:
                return False

        wait_for(probe, deadline=deadline, message=f"{label} never became ready at {url}")

    def _wait_backend_census(self, deadline: float) -> None:
        # The bus census lists ALL origins (serve + backend), so readiness keys on
        # the origin KIND: the backend worker is up once a ``backend``-kind origin
        # appears, not merely once the census is non-empty (the serve workers
        # register first).
        def probe() -> bool:
            early = self._early_exit_detail()
            if early is not None:
                raise RuntimeError(f"backend census: {early}")
            return any(origin.kind == "backend" for origin in self.census())

        wait_for(probe, deadline=deadline, message="no backend-kind origin ever appeared in the worker-bus census")

    def _run_readiness_coro(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a readiness coroutine to completion from synchronous boot/restart code.
        Boot runs outside any event loop, but ``restart`` is called from within an
        async test's running loop, so the coroutine is driven on a dedicated thread
        with its own loop — correct whether or not the calling thread already owns one,
        and its failure is re-raised on the calling thread."""
        import threading

        box: dict[str, BaseException] = {}

        def runner() -> None:
            try:
                asyncio.run(coro)
            except BaseException as exc:  # re-raised on the calling thread below
                box["exc"] = exc

        thread = threading.Thread(target=runner, name="tai-e2e-readiness")
        thread.start()
        thread.join()
        if "exc" in box:
            raise box["exc"]

    async def wait_workers(self, n: int, *, port: int | None = None, deadline: float = 10.0) -> dict[int, str]:
        """Poll the ``e2e_worker_info`` probe until ``n`` distinct worker pids have
        answered, returning each pid mapped to its reported state digest. Convergence
        assertions compare those digests: every distinct pid reporting the same digest,
        differing from the pre-mutation baseline, is fleet-wide convergence. ``port``
        selects which app port to probe (default the primary). The stack's own auth token
        authenticates the probe, so the drain reaches the fenced surface."""
        seen: dict[int, str] = {}

        async def worker_info() -> dict[str, Any]:
            async with self.mcp(port, auth=self.auth_token) as client:
                # A worker fresh in the census may still hold its boot-time reload gate
                # (the ~2s self-resync), so poll past the retriable ``reloading`` rejection.
                result = await client.call_tool("e2e_worker_info", retry_on_reloading=True)
            data = result.data if result.data is not None else result.structured_content
            if not isinstance(data, dict) or "pid" not in data or "state_digest" not in data:
                raise RuntimeError(f"e2e_worker_info returned an unexpected shape: {data!r}")
            return data

        async def probe() -> bool:
            # The MCP initialize handshake itself is rejected while the worker holds its
            # self-resync gate (the client raises before a session exists, so the tool
            # call's own ``retry_on_reloading`` cannot cover it); treat that envelope as
            # "not ready yet" and keep polling.
            data = await _probe_tolerating_reloading(worker_info)
            if data is None:
                return False
            seen[int(data["pid"])] = str(data["state_digest"])
            return len(seen) >= n

        await wait_for_async(probe, deadline=deadline, message=f"only saw {sorted(seen)} of {n} workers")
        return seen

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

    def census(self) -> list[BusOrigin]:
        """The live fleet currently on the app-owned worker bus — every subscribed
        origin (HTTP ``serve`` workers AND the ``backend`` runtime), scanned off the
        bus presence keys under this stack's namespace. Backend-independent."""
        # Local import: variants.py imports this module, so the census helper is
        # reached at call time to avoid a module-load cycle.
        from tai42_e2e.variants import bus_census

        return bus_census(self.resources.bus_redis_url, self.resources.bus_namespace)

    def records(self, key: str) -> list[str]:
        """The raw JSON strings ``e2e_record`` RPUSH'd under ``key``."""
        return self.infra.redis.records(key)

    def process(self, name: str) -> ProcessHandle:
        return self._procs[name]

    def restart(self, name: str) -> None:
        """Stop and respawn one process from its saved spec (component-restart
        tests). The new process re-enters readiness for its own port kind."""
        handle = self._procs[name]
        handle.terminate()
        # The respawn reuses the same port; wait for the killed process to release
        # it before rebinding, else the new process fails to bind and exits early.
        for port in self._ports_for(name):
            wait_for(lambda p=port: ports.is_free(p), deadline=5.0, message=f"port {port} never freed before restart")
        spec = self._specs[name]
        self._spawn(spec)
        self._wait_after_restart(name)

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
        self._procs[name].kill_now()

    def attach_relay(self, relay: TcpRelay) -> None:
        """Register a per-stack TCP relay for teardown reap + leak-check. The
        infra-outage tests front the stack's Redis/PG with a relay so a test can
        sever the connection mid-run; attaching it here means teardown stops it
        and asserts it left nothing listening (like a leaked port)."""
        self._relays.append(relay)

    def _wait_after_restart(self, name: str) -> None:
        deadline = self.infra.settings.boot_timeout
        if name.startswith("serve"):
            idx = 0 if name in ("serve", "serve-a") else 1
            port = self.app_ports[idx]
            self._wait_http_ok(f"http://{self.host}:{port}/health", deadline, "app health")
            # A respawned serve worker re-runs its boot self-resync gate on rejoin;
            # drain it (where the profile carries the probe) so a test acting right
            # after the restart does not race the gate, exactly as at boot.
            if self._needs_bus():
                self._run_readiness_coro(self._drain_gate_coro([port], deadline))
        elif name == "embed":
            self._wait_http_ok(f"http://{self.host}:{self.app_ports[0]}/health", deadline, "app health")
            if self._needs_bus():
                self._run_readiness_coro(self._drain_gate_coro([self.app_ports[0]], deadline))
        elif name == "metrics":
            assert self.metrics_port is not None
            self._wait_http_ok(f"http://{self.host}:{self.metrics_port}/metrics", deadline, "metrics")
        elif name == "backend":
            self._wait_backend_census(deadline)

    # ---- config rendering ------------------------------------------------

    def _render_env_file(self) -> None:
        """Render the feature env map to ``<config>/.env`` so the admin reload
        path (which re-reads ``.env``) sees the same values the process env
        does. TMPDIR is deliberately NOT written here — it is per-process, which
        is how REPLICAS get per-replica metrics dirs from one shared .env."""
        merged = {
            **self.config.env,
            **self._origin_allowlist_env(),
            **self._replica_b_origin_env(),
            **self._bus_env(),
        }
        lines = [f"{key}={value}" for key, value in sorted(merged.items())]
        (self._config_dir / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _origin_allowlist_env(self) -> dict[str, str]:
        """Fill each ``origin_allowlist_env_keys`` entry with this stack's own
        loopback app origins (``http://host:port`` for every allocated app port,
        comma-joined). Empty until boot allocates the ports."""
        if not self.config.origin_allowlist_env_keys or not self.app_ports:
            return {}
        origins = ",".join(f"http://{self.host}:{port}" for port in self.app_ports)
        return dict.fromkeys(self.config.origin_allowlist_env_keys, origins)

    def _needs_bus(self) -> bool:
        """Whether this stack joins the app-owned worker bus. The SUT refuses to boot
        busless under ``--workers > 1`` or a registered backend; a file-mode REPLICAS stack
        (each master ``--workers 1``) would boot busless but serve stale, so wiring the bus
        there is harness policy, not a SUT rule. The embed host rides the same rule through
        its backend worker."""
        return self.config.workers > 1 or self.config.run_backend or self.config.topology is Topology.REPLICAS

    def _bus_env(self) -> dict[str, str]:
        """Point the worker bus at this stack's own bus Redis endpoint under a
        per-stack namespace. The namespace is mandatory: bus pub/sub channels +
        presence keys are server-global, so co-tenant stacks on one Redis MUST
        diverge by namespace or they cross-deliver each other's fleet ops and
        cross-count each other's census."""
        if not self._needs_bus():
            return {}
        return {
            "TAI_BUS_REDIS_URL": self.resources.bus_redis_url,
            "TAI_BUS_NAMESPACE": self.resources.bus_namespace,
        }

    def _replica_b_origin_env(self) -> dict[str, str]:
        """Fill each ``replica_b_origin_env_keys`` entry with this stack's SINGLE
        replica-B loopback origin (``http://host:port_b``), only known after boot
        allocates the ports. Requires a two-app-port (REPLICAS) topology — a
        profile that names these keys on a single-port stack is a configuration
        error, raised loudly rather than silently pointing them at replica A."""
        keys = self.config.replica_b_origin_env_keys
        if not keys:
            return {}
        if len(self.app_ports) < 2:
            raise RuntimeError(
                "replica_b_origin_env_keys requires a REPLICAS topology (two app ports); "
                f"stack {self.config.name!r} has {len(self.app_ports)}"
            )
        origin = f"http://{self.host}:{self.app_ports[1]}"
        return dict.fromkeys(keys, origin)
