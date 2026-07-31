"""Boot rules: the SUT refuses to start every busless deployment shape that needs the
worker bus, and the refusal is loud and NAMES ``TAI_BUS_REDIS_URL``.

Each refusal spawns a REAL entrypoint (``tai serve`` / ``tai backend worker`` / a
factory-string ``uvicorn``) against a bus-requiring config with no
``TAI_BUS_REDIS_URL`` and asserts a nonzero exit whose stderr names the setting —
driven through :func:`spawn_expect_refusal`, which runs the process OUTSIDE the
stack readiness framework (a process that serves instead of refusing is the
failure). The one supported busless shape — a single-worker, file-mode, no-backend
server — is the positive control and boots through the normal stack path.

Backend-agnostic: no scenario runs a backend worker or exercises a backend seam, so
the whole module is ``backendless``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tai42_e2e.stack import TaiStack, spawn_expect_refusal, tai_bin, uvicorn_bin

pytestmark = pytest.mark.backendless

# The one setting every busless-refusal message must name.
_BUS_VAR = "TAI_BUS_REDIS_URL"

# A backend module string for the backend-bearing manifests. It is never imported:
# the backend-needs-bus rule fires at the ``app_context`` seam (and in ``tai
# backend`` right after the manifest read) BEFORE any backend module load, so the
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


def test_multiworker_serve_without_bus_refuses(tmp_path: Path) -> None:
    """A1: ``tai serve -w 4`` with no bus refuses nonzero, naming the setting.

    ``--stateless-http`` is required for a multi-worker http server (a stateful
    transport is rejected first, on a different rule); the bus rule is what fires
    here."""
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


def test_k8s_mode_without_bus_refuses_naming_the_setting(tmp_path: Path) -> None:
    """A3: a ``TAI_CONFIG_MODE=k8s`` boot with no bus refuses naming the setting. The
    check runs BEFORE the config manager is constructed, so the stderr carries the
    bus-setting refusal and NO kubeconfig / ConfigMap connection error precedes it."""
    config_dir, manifest = _seed_config(tmp_path, backend=False)
    env = _busless_env(tmp_path, config_dir, manifest, config_mode="k8s")
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
    assert _BUS_VAR in stderr, stderr
    assert "k8s config mode" in stderr, stderr
    # The check preceded config-manager construction: no kube/ConfigMap error leaked.
    lowered = stderr.lower()
    assert "configmap" not in lowered, stderr
    assert "kubeconfig" not in lowered, stderr


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
