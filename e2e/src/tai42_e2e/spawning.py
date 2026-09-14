"""Process spawning for the boot engine: staging run-family metrics dirs and
launching the ``tai serve`` fleet, the embed host, the backend worker (plus any
variant-required backend siblings), and the metrics server."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tai42_e2e.binaries import tai_bin, uvicorn_bin
from tai42_e2e.child_env import child_env
from tai42_e2e.procs import ProcessHandle
from tai42_e2e.topology import Topology, _ProcSpec

if TYPE_CHECKING:
    from tai42_e2e.stack import TaiStack


def make_family_dirs(root: Path, n_app: int) -> list[str]:
    dirs: list[str] = []
    for i in range(n_app):
        family = root / f"tmp-{chr(ord('a') + i)}"
        (family / "tai42_prometheus").mkdir(parents=True, exist_ok=True)
        dirs.append(str(family))
    return dirs


def spawn(stack: TaiStack, spec: _ProcSpec) -> None:
    handle = ProcessHandle(name=spec.name, argv=spec.argv, cwd=spec.cwd, env=spec.env, log_path=spec.log_path)
    stack._specs[spec.name] = spec
    stack._procs[spec.name] = handle
    handle.start()


def spawn_all(stack: TaiStack, manifest_path: Path, family_dirs: list[str]) -> None:
    tai = tai_bin()
    if stack.config.embed:
        spawn_embed_host(stack, family_dirs[0])
    else:
        spawn_serve_fleet(stack, tai, manifest_path, family_dirs)

    # The backend worker + metrics server join run-family "a".
    if stack.config.run_backend:
        name = "backend"
        cwd_override = stack.config.cwd_overrides.get(name)
        cwd = Path(cwd_override) if cwd_override else stack._config_dir
        spawn(
            stack,
            _ProcSpec(
                name=name,
                argv=[tai, "backend", "worker", "--manifest-path", str(manifest_path)],
                cwd=cwd,
                env=child_env(stack, family_dirs[0], cwd_override),
                log_path=stack._logs_dir / "backend.log",
            ),
        )
        # Extra backend processes the variant requires alongside the worker
        # (celery's RedBeat / rq's rq-scheduler; arq needs none). Each is a
        # full ``tai backend <args>`` process with its own ProcessHandle, log,
        # and teardown leak-reap — an early exit aborts boot loudly through the
        # shared ``_early_exit_detail`` readiness check, exactly like the worker.
        for extra_args in stack.infra.variants.backend.extra_backend_processes():
            extra_name = f"backend-{extra_args[0]}"
            extra_cwd = stack.config.cwd_overrides.get(extra_name)
            spawn(
                stack,
                _ProcSpec(
                    name=extra_name,
                    argv=[tai, "backend", *extra_args, "--manifest-path", str(manifest_path)],
                    cwd=Path(extra_cwd) if extra_cwd else stack._config_dir,
                    env=child_env(stack, family_dirs[0], extra_cwd),
                    log_path=stack._logs_dir / f"{extra_name}.log",
                ),
            )
    if stack.config.run_metrics:
        assert stack.metrics_port is not None
        name = "metrics"
        cwd_override = stack.config.cwd_overrides.get(name)
        cwd = Path(cwd_override) if cwd_override else stack._config_dir
        spawn(
            stack,
            _ProcSpec(
                name=name,
                argv=[tai, "metrics", "--host", stack.host, "--port", str(stack.metrics_port)],
                cwd=cwd,
                env=child_env(stack, family_dirs[0], cwd_override),
                log_path=stack._logs_dir / "metrics.log",
            ),
        )


def spawn_serve_fleet(stack: TaiStack, tai: str, manifest_path: Path, family_dirs: list[str]) -> None:
    """Spawn the ``tai serve`` fleet — one master per app port, honouring the
    stack's topology (MULTIWORKER: ``--workers N`` on one port; REPLICAS: two
    one-worker masters on two ports)."""
    n_app = len(stack.app_ports)
    for i, port in enumerate(stack.app_ports):
        name = "serve" if n_app == 1 else f"serve-{chr(ord('a') + i)}"
        workers = stack.config.workers if stack.config.topology is Topology.MULTIWORKER else 1
        argv = [
            tai,
            "serve",
            "--host",
            stack.host,
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
        cwd_override = stack.config.cwd_overrides.get(name)
        cwd = Path(cwd_override) if cwd_override else stack._config_dir
        spawn(
            stack,
            _ProcSpec(
                name=name,
                argv=argv,
                cwd=cwd,
                env=child_env(stack, family_dirs[i], cwd_override),
                log_path=stack._logs_dir / f"{name}.log",
            ),
        )


def spawn_embed_host(stack: TaiStack, family_dir: str) -> None:
    """Spawn the user-owned embed host: ``uvicorn`` serving the host FastAPI
    app in ``tai42_e2e_fixtures.embed_main`` that mounts ``create_app()``. One process
    on the single app port; the clean child env carries no ``PROMETHEUS_MULTIPROC_DIR``,
    so the mounted app comes up in in-process metrics mode — the surface the embed
    suite scrapes."""
    name = "embed"
    port = stack.app_ports[0]
    cwd_override = stack.config.cwd_overrides.get(name)
    cwd = Path(cwd_override) if cwd_override else stack._config_dir
    argv = [
        uvicorn_bin(),
        "tai42_e2e_fixtures.embed_main:app",
        "--host",
        stack.host,
        "--port",
        str(port),
    ]
    spawn(
        stack,
        _ProcSpec(
            name=name,
            argv=argv,
            cwd=cwd,
            env=child_env(stack, family_dir, cwd_override),
            log_path=stack._logs_dir / f"{name}.log",
        ),
    )
