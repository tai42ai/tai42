"""Boot rules: the SUT refuses to start every busless deployment shape that needs the
worker bus, and the refusal is loud and NAMES ``TAI_BUS_REDIS_URL``; it also refuses a
bus-enabled config whose fleet apply window is below the retire's drain floor, naming
both settings and the floor, and a multi-worker server with no shared sub-MCP
registration store, naming ``SUB_MCP_REDIS_URL``, or no shared hooks registry, naming
``HOOKS_REDIS_URL``, including when the config-dir ``.env`` empties the bus or a store
URL the process env sets; and a config that registers a task backend over an in-memory hooks
registry, naming ``HOOKS_REDIS_URL``, from both ``tai serve`` and ``tai backend worker``.

Each refusal spawns a REAL entrypoint (``tai serve`` / ``tai backend worker`` / a
factory-string ``uvicorn``) and asserts a nonzero exit whose stderr names the refused
setting — driven through :func:`spawn_expect_refusal`, which runs the process OUTSIDE
the stack readiness framework (a process that serves instead of refusing is the
failure). The busless refusals run with no ``TAI_BUS_REDIS_URL`` (:func:`_busless_env`);
the other refusals configure a bus URL that is never dialled (:func:`_bus_env`), since
each fires before the process joins the bus. The one supported busless shape — a
single-worker, file-mode, no-backend server — is the positive control and boots through
the normal stack path. A multi-worker server whose bus and stores are kept only in the
config-dir ``.env`` boots too, since each worker reads its boot rules under the stored env.

Backend-agnostic: no scenario runs a backend worker or exercises a backend seam, so
the whole module is ``backendless``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import redis

from tai42_e2e.binaries import spawn_expect_refusal, tai_bin, uvicorn_bin
from tai42_e2e.ports import allocate_port, release_port
from tai42_e2e.stack import TaiStack
from tai42_e2e.topology import Infra
from tai42_e2e.waiting import wait_for

pytestmark = pytest.mark.backendless

# The one setting every busless-refusal message must name.
_BUS_VAR = "TAI_BUS_REDIS_URL"

# A backend module string for the backend-bearing manifests. It is never imported:
# the backend-needs-bus and backend-needs-hooks-store rules fire at the
# ``app_context`` seam (and in ``tai backend`` right after the manifest read)
# BEFORE any backend module load, so the
# string only has to be truthy for the manifest to "register a task backend".
_UNREACHED_BACKEND = "boot_rules_probe.backend_never_imported"


def _venv_path() -> str:
    """PATH with the active venv's bin dir first — the real entrypoints resolve
    their siblings from it, matching a clean child env."""
    return os.pathsep.join([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"])


def _seed_config(tmp_path: Path, *, backend: bool) -> tuple[Path, Path]:
    """Seed a config dir with a manifest that mounts only the health router, adding
    a ``backend_module`` when ``backend`` — the shape the backend-needs-bus rule
    reads. Returns ``(config_dir, manifest_path)``."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    lines = ["routers_modules:", "  - tai42_skeleton.routers.health"]
    if backend:
        lines.append(f"backend_module: {_UNREACHED_BACKEND}")
    manifest = config_dir / "manifest.yml"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_dir, manifest


def _busless_env(
    tmp_path: Path,
    config_dir: Path,
    manifest: Path,
    *,
    config_mode: str = "file",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """A clean child env pointing the entrypoint at the seeded config, with NO
    ``TAI_BUS_REDIS_URL`` — the refusal condition is precisely the setting's
    absence. TMPDIR is a real writable dir (the serve path stakes its Prometheus
    multiproc dir under it)."""
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(exist_ok=True)
    env = {
        "PATH": _venv_path(),
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "TMPDIR": str(tmpdir),
        "TAI_CONFIG_MODE": config_mode,
        "TAI_CONFIG_DIR_PATH": str(config_dir),
        "TAI_MANIFEST_PATH": str(manifest),
    }
    if extra:
        env.update(extra)
    assert _BUS_VAR not in env, "the busless-refusal env must not configure the bus"
    return env


# A bus URL no test dials: every rule that :func:`_bus_env` serves fires before the
# process joins the bus.
_UNDIALLED_REDIS = "redis://127.0.0.1:1/0"


def _bus_env(tmp_path: Path, config_dir: Path, manifest: Path) -> dict[str, str]:
    """The :func:`_busless_env` child env plus a ``TAI_BUS_REDIS_URL`` that is never
    dialled, for the refusals that need the bus rules satisfied to reach their own."""
    env = _busless_env(tmp_path, config_dir, manifest)
    env[_BUS_VAR] = _UNDIALLED_REDIS
    return env


@pytest.mark.needs("no-stack", "process", "cli", "files", "setting:TAI_BUS_REDIS_URL=unset")
def test_multiworker_serve_without_bus_refuses(tmp_path: Path) -> None:
    """A1: ``tai serve -w 4`` with no bus refuses nonzero, naming the setting.

    ``--stateless-http`` is required for a multi-worker http server (a stateful
    transport is rejected first, on a different rule); the bus rule is what fires
    here. Each worker refuses at its boot, after the stored env is applied, and the
    master exits with the workers' startup-failure status."""
    config_dir, manifest = _seed_config(tmp_path, backend=False)
    env = _busless_env(tmp_path, config_dir, manifest)
    argv = [
        tai_bin(),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--workers",
        "4",
        "--stateless-http",
        "--manifest-path",
        str(manifest),
    ]
    stderr = spawn_expect_refusal(argv, env, config_dir)
    assert _BUS_VAR in stderr, stderr
    assert "workers" in stderr, stderr


@pytest.mark.parametrize(
    "argv_tail",
    [
        pytest.param(["serve", "--host", "127.0.0.1", "--port", "0", "--workers", "1"], id="serve"),
        pytest.param(["backend", "worker"], id="backend"),
    ],
)
@pytest.mark.needs("no-stack", "process", "cli", "files", "setting:TAI_BUS_REDIS_URL=unset")
def test_backend_manifest_without_bus_refuses(tmp_path: Path, argv_tail: list[str]) -> None:
    """A2: a manifest that registers a backend with no bus refuses nonzero and names
    the setting — for both ``tai serve`` (the ``app_context`` seam) and ``tai
    backend`` (the post-manifest-read check)."""
    config_dir, manifest = _seed_config(tmp_path, backend=True)
    env = _busless_env(tmp_path, config_dir, manifest)
    argv = [tai_bin(), *argv_tail, "--manifest-path", str(manifest)]
    stderr = spawn_expect_refusal(argv, env, config_dir)
    assert _BUS_VAR in stderr, stderr
    # The backend-needs-bus rule specifically, not another refusal.
    assert "task backend" in stderr, stderr
    assert _UNREACHED_BACKEND in stderr, stderr


@pytest.mark.needs(
    "no-stack", "process", "cli", "files", "setting:TAI_BUS_REDIS_URL=unset", "setting:TAI_CONFIG_MODE=external"
)
def test_shared_config_mode_without_bus_refuses_naming_the_setting(tmp_path: Path) -> None:
    """A3: a non-file ``TAI_CONFIG_MODE`` (external provider) boot with no bus refuses
    naming the setting. The check runs BEFORE the config manager is constructed, so the
    stderr carries the bus-setting refusal and NO provider connection error precedes it."""
    config_dir, manifest = _seed_config(tmp_path, backend=False)
    env = _busless_env(tmp_path, config_dir, manifest, config_mode="external")
    argv = [
        tai_bin(),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--workers",
        "1",
        "--manifest-path",
        str(manifest),
    ]
    stderr = spawn_expect_refusal(argv, env, config_dir)
    # The refusal names the bus var and the active mode, and — since it precedes
    # config-manager construction — is a clean boot-rule refusal, not a provider
    # import/connection error.
    assert _BUS_VAR in stderr, stderr
    assert "external" in stderr, stderr


@pytest.mark.needs(
    "no-stack", "process", "files", "setting:TAI_BUS_REDIS_URL=unset", "setting:uvicorn-factory-entrypoint"
)
def test_factory_string_backend_without_bus_refuses(tmp_path: Path) -> None:
    """A5: the second enforcement seam — a factory-string ``uvicorn`` on
    ``tai42_skeleton.cli.mcp_app:create_app`` (bypassing the CLI) with a
    backend-bearing manifest and no bus. The ASGI lifespan fails loudly naming the
    setting, and uvicorn surfaces the startup failure as a nonzero exit.

    A CLI-bypassing process manager stamps ``PROMETHEUS_MULTIPROC_DIR`` itself (the
    CLI stamps it before spawning its uvicorn workers); without it the factory would
    trip the metrics-backend assert before reaching the lifespan bus rule, so the
    env sets it to reach the seam under test."""
    config_dir, manifest = _seed_config(tmp_path, backend=True)
    multiproc = tmp_path / "multiproc"
    multiproc.mkdir()
    env = _busless_env(tmp_path, config_dir, manifest, extra={"PROMETHEUS_MULTIPROC_DIR": str(multiproc)})
    argv = [
        uvicorn_bin(),
        "--factory",
        "tai42_skeleton.cli.mcp_app:create_app",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
    ]
    stderr = spawn_expect_refusal(argv, env, config_dir)
    assert _BUS_VAR in stderr, stderr
    assert "task backend" in stderr, stderr


@pytest.mark.needs(
    "no-stack",
    "process",
    "cli",
    "files",
    "setting:TAI_BUS_APPLY_TIMEOUT=20",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=unset",
)
def test_apply_window_below_the_drain_floor_refuses(tmp_path: Path) -> None:
    """A6: ``tai serve`` with the bus configured, a 20 s fleet apply window and the default
    10 s drain budget refuses nonzero: a reload's retire may legally spend three drains
    (30 s), so the refusal names both settings and the floor. The rule fires at the
    ``app_context`` seam before the process joins the bus, so the configured bus URL is
    never dialled."""
    config_dir, manifest = _seed_config(tmp_path, backend=False)
    env = _bus_env(tmp_path, config_dir, manifest)
    env["TAI_BUS_APPLY_TIMEOUT"] = "20"
    argv = [
        tai_bin(),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--workers",
        "1",
        "--manifest-path",
        str(manifest),
    ]
    stderr = spawn_expect_refusal(argv, env, config_dir)
    assert "TAI_BUS_APPLY_TIMEOUT=20" in stderr, stderr
    assert "TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=10" in stderr, stderr
    assert "30 s" in stderr, stderr


@pytest.mark.needs(
    "no-stack",
    "process",
    "cli",
    "files",
    "setting:SUB_MCP_REDIS_URL=unset",
    "setting:TAI_DEFAULT_REDIS_URL=unset",
)
def test_multiworker_serve_without_sub_mcp_store_refuses(tmp_path: Path) -> None:
    """``tai serve -w 4`` with the bus configured but no shared sub-MCP registration
    store refuses nonzero, naming ``SUB_MCP_REDIS_URL``: the in-memory store is
    per-process, so a sub-MCP app registered through one worker would not be served by
    its siblings. Each worker refuses at its boot, after the stored env is applied and
    before it joins the bus, so the configured bus URL is never dialled, and the master
    exits with the workers' startup-failure status."""
    config_dir, manifest = _seed_config(tmp_path, backend=False)
    env = _bus_env(tmp_path, config_dir, manifest)
    argv = [
        tai_bin(),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--workers",
        "4",
        "--stateless-http",
        "--manifest-path",
        str(manifest),
    ]
    stderr = spawn_expect_refusal(argv, env, config_dir)
    assert "SUB_MCP_REDIS_URL" in stderr, stderr
    assert "4 workers" in stderr, stderr


@pytest.mark.needs(
    "no-stack",
    "process",
    "cli",
    "files",
    "setting:HOOKS_REDIS_URL=unset",
    "setting:TAI_DEFAULT_REDIS_URL=unset",
)
def test_multiworker_serve_without_hooks_store_refuses(tmp_path: Path) -> None:
    """``tai serve -w 4`` with the bus and the sub-MCP store configured but no shared hooks
    registry refuses nonzero, naming ``HOOKS_REDIS_URL``: the in-memory registry is
    per-process, so a hook registered through one worker would not run for the events its
    siblings accept. Each worker refuses at its boot, before it joins the bus or opens a
    store, so no configured Redis URL is ever dialled."""
    config_dir, manifest = _seed_config(tmp_path, backend=False)
    env = _bus_env(tmp_path, config_dir, manifest)
    env["SUB_MCP_REDIS_URL"] = _UNDIALLED_REDIS
    argv = [
        tai_bin(),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--workers",
        "4",
        "--stateless-http",
        "--manifest-path",
        str(manifest),
    ]
    stderr = spawn_expect_refusal(argv, env, config_dir)
    assert "HOOKS_REDIS_URL" in stderr, stderr
    assert "4 workers" in stderr, stderr


@pytest.mark.parametrize(
    ("store_var", "other_store_var"),
    [
        pytest.param("SUB_MCP_REDIS_URL", "HOOKS_REDIS_URL", id="sub-mcp"),
        pytest.param("HOOKS_REDIS_URL", "SUB_MCP_REDIS_URL", id="hooks"),
    ],
)
@pytest.mark.needs("no-stack", "process", "cli", "files", "setting:TAI_DEFAULT_REDIS_URL=unset")
def test_multiworker_serve_refuses_a_store_url_the_stored_env_empties(
    tmp_path: Path, store_var: str, other_store_var: str
) -> None:
    """``tai serve -w 2`` whose process env sets a store URL that the config-dir ``.env``
    sets empty refuses nonzero, naming the setting: the stored env overrides the process
    env at each worker's boot, so the store the workers would open is in-memory. Each
    worker refuses before it joins the bus or opens a store, so no configured Redis URL
    is ever dialled."""
    config_dir, manifest = _seed_config(tmp_path, backend=False)
    (config_dir / ".env").write_text(f"{store_var}=\n", encoding="utf-8")
    env = _bus_env(tmp_path, config_dir, manifest)
    env[store_var] = _UNDIALLED_REDIS
    env[other_store_var] = _UNDIALLED_REDIS
    argv = [
        tai_bin(),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--workers",
        "2",
        "--stateless-http",
        "--manifest-path",
        str(manifest),
    ]
    stderr = spawn_expect_refusal(argv, env, config_dir)
    assert store_var in stderr, stderr
    assert "2 workers" in stderr, stderr


@pytest.mark.needs("no-stack", "process", "cli", "files", "setting:TAI_DEFAULT_REDIS_URL=unset")
def test_multiworker_serve_refuses_a_bus_url_the_stored_env_empties(tmp_path: Path) -> None:
    """``tai serve -w 2`` whose process env sets the bus URL that the config-dir ``.env``
    sets empty refuses nonzero, naming the setting: the stored env overrides the process
    env at each worker's boot, so every worker would come up on its own local bus. Each
    worker refuses before it joins a bus or opens a store, so no configured Redis URL is
    ever dialled."""
    config_dir, manifest = _seed_config(tmp_path, backend=False)
    (config_dir / ".env").write_text(f"{_BUS_VAR}=\n", encoding="utf-8")
    env = _bus_env(tmp_path, config_dir, manifest)
    env["SUB_MCP_REDIS_URL"] = _UNDIALLED_REDIS
    env["HOOKS_REDIS_URL"] = _UNDIALLED_REDIS
    argv = [
        tai_bin(),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--workers",
        "2",
        "--stateless-http",
        "--manifest-path",
        str(manifest),
    ]
    stderr = spawn_expect_refusal(argv, env, config_dir)
    assert _BUS_VAR in stderr, stderr
    assert "2 workers" in stderr, stderr


@pytest.mark.needs("process", "cli", "files", "store:redis", "setting:TAI_DEFAULT_REDIS_URL=unset")
def test_multiworker_serve_boots_with_its_bus_only_in_the_stored_env(tmp_path: Path, infra: Infra) -> None:
    """``tai serve -w 2`` whose process env carries no bus and no store URL, with all
    three kept only in the config-dir ``.env``, boots and serves: each worker applies the
    stored env before its boot rules, so both workers join the bus the stored env names."""
    config_dir, manifest = _seed_config(tmp_path, backend=False)
    db = infra.redis.allocate_db()
    port = allocate_port()
    try:
        url = infra.redis.url_for(db)
        (config_dir / ".env").write_text(
            f"{_BUS_VAR}={url}\nSUB_MCP_REDIS_URL={url}\nHOOKS_REDIS_URL={url}\n", encoding="utf-8"
        )
        env = _busless_env(tmp_path, config_dir, manifest, extra={"ACCESS_CONTROL_ENABLE": "false"})
        argv = [
            tai_bin(),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "2",
            "--stateless-http",
            "--manifest-path",
            str(manifest),
        ]
        # Run from a directory with no ``.env``: ``tai serve`` loads a ``.env`` from its working
        # directory into its own env, which would hand the bus to the master too.
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        log_path = tmp_path / "serve.log"
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(argv, env=env, cwd=str(run_dir), stdout=log, stderr=log, start_new_session=True)
        try:

            def joined() -> int | None:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"tai serve exited {proc.returncode} instead of serving:\n{log_path.read_text()}"
                    )
                presences = _presence_keys(infra, db)
                return presences if presences == 2 else None

            wait_for(joined, deadline=60.0, message="both workers never joined the bus the stored env names")
            health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5.0)
            assert health.status_code == 200, f"{health.status_code} {health.text}"
            assert health.text.strip() == "OK"
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=20.0)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5.0)
    finally:
        release_port(port)
        infra.redis.release_db(db)


def _presence_keys(infra: Infra, db: int) -> int:
    """The number of workers whose bus presence key is live on Redis logical DB ``db``."""
    client = redis.Redis.from_url(infra.redis.url_for(db))
    try:
        return sum(1 for _ in client.scan_iter(match="*:bus:presence:*"))
    finally:
        client.close()


@pytest.mark.needs("process", "store:redis", "setting:TAI_BUS_REDIS_URL=unset")
async def test_single_worker_filemode_no_backend_boots(bare_stack: TaiStack) -> None:
    """A4 (positive control): a single-worker, file-mode, no-backend server has no
    bus configured and BOOTS and serves — the one supported busless shape, reached
    through the normal stack readiness path, not the refusal helper.

    ``bare_stack`` is MULTIWORKER(1) with no backend worker, so the harness wires it
    NO bus env; the served process therefore runs on the no-op local bus and joins
    no fleet (an empty census)."""
    # The served process carries no bus setting — this is genuinely the busless shape.
    assert _BUS_VAR not in bare_stack.process("serve").env
    # No origin ever joined the bus (the local no-op bus registers no presence).
    assert bare_stack.census() == []
    # And it serves.
    api = bare_stack.api()
    health = await api.request_raw("GET", "/health")
    assert health.status_code == 200, f"busless single-worker server not healthy: {health.status_code} {health.text}"
    assert health.text.strip() == "OK"


@pytest.mark.parametrize(
    "argv_tail",
    [
        pytest.param(["serve", "--host", "127.0.0.1", "--port", "0", "--workers", "1"], id="serve"),
        pytest.param(["backend", "worker"], id="backend"),
    ],
)
@pytest.mark.needs(
    "no-stack",
    "process",
    "cli",
    "files",
    "setting:HOOKS_REDIS_URL=unset",
    "setting:TAI_DEFAULT_REDIS_URL=unset",
)
def test_backend_manifest_without_hooks_store_refuses(tmp_path: Path, argv_tail: list[str]) -> None:
    """A manifest that registers a backend, with the bus configured but no shared hooks
    registry, refuses nonzero and names ``HOOKS_REDIS_URL`` — for both ``tai serve`` (the
    ``app_context`` seam) and ``tai backend`` (the post-manifest-read check). The backend
    process matches the events it raises against its own registry, and an in-memory one
    holds none of the hooks registered through the server. The rule fires before the bus
    is joined, so the configured bus URL is never dialled."""
    config_dir, manifest = _seed_config(tmp_path, backend=True)
    env = _bus_env(tmp_path, config_dir, manifest)
    argv = [tai_bin(), *argv_tail, "--manifest-path", str(manifest)]
    stderr = spawn_expect_refusal(argv, env, config_dir)
    # The backend-needs-hooks-store rule specifically: the in-memory warning also names
    # the setting, and a later boot step would fail on the never-imported backend module.
    assert f"Refusing a config that registers a task backend ({_UNREACHED_BACKEND!r})" in stderr, stderr
    assert "in-memory hooks registry" in stderr, stderr
    assert "HOOKS_REDIS_URL" in stderr, stderr
