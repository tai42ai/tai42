"""Child-process env construction for the boot engine: the clean per-child env,
the rendered ``.env`` file, and the after-boot origin/bus fills a profile names
but only the booted stack can resolve to concrete ports."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_e2e.stack import TaiStack
    from tai42_e2e.topology import StackConfig, StackResources


def needs_bus(config: StackConfig) -> bool:
    """Whether this stack joins the app-owned worker bus. The SUT refuses to boot
    busless under ``--workers > 1`` or a registered backend; a file-mode REPLICAS stack
    (each master ``--workers 1``) would boot busless but serve stale, so wiring the bus
    there is harness policy, not a SUT rule. The embed host rides the same rule through
    its backend worker."""
    from tai42_e2e.topology import Topology

    return config.workers > 1 or config.run_backend or config.topology is Topology.REPLICAS


def bus_env(config: StackConfig, resources: StackResources) -> dict[str, str]:
    """Point the worker bus at this stack's own bus Redis endpoint under a
    per-stack namespace. The namespace is mandatory: bus pub/sub channels +
    presence keys are server-global, so co-tenant stacks on one Redis MUST
    diverge by namespace or they cross-deliver each other's fleet ops and
    cross-count each other's census."""
    if not needs_bus(config):
        return {}
    return {
        "TAI_BUS_REDIS_URL": resources.bus_redis_url,
        "TAI_BUS_NAMESPACE": resources.bus_namespace,
    }


def origin_allowlist_env(config: StackConfig, host: str, app_ports: list[int]) -> dict[str, str]:
    """Fill each ``origin_allowlist_env_keys`` entry, only known after boot.

    Mock legs (the default) fill every entry with this stack's own loopback app
    origins (``http://host:port`` for every allocated app port, comma-joined).

    A REAL connector leg additionally lists keys in ``public_allowlist_env_keys``:
    those fill from ``E2E_PUBLIC_BASE_URL`` (the origin the vendor redirects the OAuth
    consent back to) and OVERRIDE the loopback fill, so the same key the connect flow
    validates the request-derived redirect_uri against carries the public origin the
    real redirect is registered at. The leg refuses to start (loud) if it asks for
    public mode without the URL. Empty until boot allocates the ports."""
    if not config.origin_allowlist_env_keys or not app_ports:
        return {}
    public_keys = set(config.public_allowlist_env_keys)
    env: dict[str, str] = {}
    if public_keys:
        if not config.public_base_url:
            raise RuntimeError(
                f"stack {config.name!r} routes {sorted(public_keys)} to the public redirect "
                "origin but public_base_url is unset (set E2E_PUBLIC_BASE_URL for a real connector leg)"
            )
        env.update(dict.fromkeys(public_keys, config.public_base_url.rstrip("/")))
    loopback_keys = [key for key in config.origin_allowlist_env_keys if key not in public_keys]
    if loopback_keys:
        origins = ",".join(f"http://{host}:{port}" for port in app_ports)
        env.update(dict.fromkeys(loopback_keys, origins))
    return env


def app_origin_env(config: StackConfig, host: str, app_ports: list[int]) -> dict[str, str]:
    """Fill each ``app_origin_env_keys`` entry with this stack's own single app
    origin (``http://host:port``), only known after boot allocates the port. Used by a
    single-port (MULTIWORKER) stack for an env key that must name its OWN origin — e.g.
    the ask callback base the web channel's answer door forwards an answer back to."""
    if not config.app_origin_env_keys:
        return {}
    if not app_ports:
        raise RuntimeError(f"app_origin_env_keys needs an allocated app port; stack {config.name!r} has none")
    origin = f"http://{host}:{app_ports[0]}"
    return dict.fromkeys(config.app_origin_env_keys, origin)


def replica_b_origin_env(config: StackConfig, host: str, app_ports: list[int]) -> dict[str, str]:
    """Fill the public-base-URL env keys, only known after boot.

    Mock legs (the default) fill each ``replica_b_origin_env_keys`` entry with
    this stack's SINGLE replica-B loopback origin (``http://host:port_b``);
    this requires a two-app-port (REPLICAS) topology — a profile that names
    these keys on a single-port stack is a configuration error, raised loudly
    rather than silently pointing them at replica A.

    A REAL inbound leg additionally lists keys in ``public_base_url_env_keys``:
    those fill from ``E2E_PUBLIC_BASE_URL`` (the vendor-reachable origin) and
    OVERRIDE the loopback fill, so the same key that reaches the door over
    loopback in a mock leg reaches it over the public origin in a real one.
    The leg refuses to start (loud) if it asks for public mode without the URL."""
    public_keys = set(config.public_base_url_env_keys)
    loopback_keys = [key for key in config.replica_b_origin_env_keys if key not in public_keys]
    if not public_keys and not loopback_keys:
        return {}
    env: dict[str, str] = {}
    if public_keys:
        if not config.public_base_url:
            raise RuntimeError(
                f"stack {config.name!r} routes {sorted(public_keys)} to the public callback "
                "origin but public_base_url is unset (set E2E_PUBLIC_BASE_URL for a real inbound leg)"
            )
        env.update(dict.fromkeys(public_keys, config.public_base_url.rstrip("/")))
    if loopback_keys:
        if len(app_ports) < 2:
            raise RuntimeError(
                "replica_b_origin_env_keys requires a REPLICAS topology (two app ports); "
                f"stack {config.name!r} has {len(app_ports)}"
            )
        origin = f"http://{host}:{app_ports[1]}"
        env.update(dict.fromkeys(loopback_keys, origin))
    return env


def _dynamic_env(stack: TaiStack) -> dict[str, str]:
    """The after-boot origin/bus fills a profile names but only the booted stack
    can resolve. The bus env lands LAST so its mandatory URL + namespace win."""
    return {
        **origin_allowlist_env(stack.config, stack.host, stack.app_ports),
        **replica_b_origin_env(stack.config, stack.host, stack.app_ports),
        **app_origin_env(stack.config, stack.host, stack.app_ports),
        **bus_env(stack.config, stack.resources),
    }


def child_env(stack: TaiStack, tmpdir: str, cwd_override: str | None) -> dict[str, str]:
    """A clean child env built from scratch: PATH/HOME/venv-bin, the stack's feature
    env, and TMPDIR for the run-family metrics dir. Never an ``os.environ`` passthrough,
    and never ``PROMETHEUS_MULTIPROC_DIR`` (the entrypoint stamps that itself)."""
    venv_bin = str(Path(sys.executable).parent)
    env: dict[str, str] = {
        "PATH": os.pathsep.join([venv_bin, "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": os.environ.get("HOME", str(stack.root)),
        "TMPDIR": tmpdir,
        "TAI_CONFIG_MODE": "file",
        "TAI_CONFIG_DIR_PATH": str(stack._config_dir),
        "TAI_MANIFEST_PATH": str(stack._config_dir / "manifest.yml"),
    }
    env.update(stack.config.env)
    env.update(_dynamic_env(stack))
    # The supervision marker is a PROCESS-env-only shape signal (never written to
    # ``.env``): it is X-band, so a profile may not carry it, and keeping it out of the
    # store means a profile built from the stored env never trips the X-band refusal —
    # yet ``detect_shape`` reads it off ``os.environ`` on every (re)spawned child.
    if stack.config.supervised:
        env["TAI_SUPERVISED"] = "harness"
    if "PROMETHEUS_MULTIPROC_DIR" in env:
        raise RuntimeError(
            "the harness must never set PROMETHEUS_MULTIPROC_DIR in a child env; "
            "the metrics dir is controlled via TMPDIR so the entrypoint stamps it"
        )
    # A per-process CWD override still needs load_dotenv to find .env, so the env
    # carries the config dir explicitly.
    _ = cwd_override
    return env


def render_env_file(stack: TaiStack) -> None:
    """Render the feature env map to ``<config>/.env`` so the admin reload
    path (which re-reads ``.env``) sees the same values the process env
    does. TMPDIR is deliberately NOT written here — it is per-process, which
    is how REPLICAS get per-replica metrics dirs from one shared .env."""
    merged = {**stack.config.env, **_dynamic_env(stack)}
    lines = [f"{key}={value}" for key, value in sorted(merged.items())]
    (stack._config_dir / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
