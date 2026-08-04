"""B7 support — the CLI stack builder and the subprocess runner.

A bare-name sibling module (the suite's convention). The stack mounts a broad
router surface with access control ON and a backend + scheduler running, so one
booted deployment answers a representative command from each ``tai`` group and the
mutating commands the plan names (presets/storage/tool_meta/schedules/roles/scopes/
keys). The runner invokes the REAL ``tai`` console script over a subprocess with
``--server`` and ``TAI_API_KEY`` (the CLI's env-var auth path; there is deliberately
no ``--api-key`` value flag), exactly as an operator drives it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tai42_e2e.manifests import (
    _CORE_ROUTERS,
    _EXTENSION_MODULES,
    _PROJECTED_API_TOOLS,
    _base_env,
    _builtin_entries,
    _memory_agent_state_env,
    _pg_env,
    _probe_tools_entry,
    _toolbox_tools_entry,
)
from tai42_e2e.stack import StackConfig, StackResources, TaiStack, Topology, tai_bin
from tai42_e2e.variants import Variants


def build_cli_stack(res: StackResources, variants: Variants) -> StackConfig:
    """REPLICAS + backend + metrics, access control ON — the CLI-coverage home.

    Mounts ``_CORE_ROUTERS`` (tools/presets/storage/tool_meta/schedules/config/
    manifest/... plus the backend door) alongside the access-control router
    (``api_keys`` — it registers ``/api/auth/{scopes,roles,api-keys,...}``, the
    keys/roles/scopes groups), the login aggregator, the resources door, and the
    system-kinds door, so one stack answers a read for every group the CLI spec
    touches. The backend runs with the ``schedule_task`` probe branch (and the
    backend's scheduler process) so ``run_tool_schedule_task`` exists for the
    schedules-add mutation. The ``seed_auth`` bootstrap mints the root key the spec
    authenticates as and maps every route to a scope the root satisfies."""
    manifest = {
        "default_routers": "none",
        "lifecycle_modules": [variants.identity.lifecycle_module],
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.api_keys",
            "tai42_skeleton.routers.login",
            "tai42_skeleton.routers.resources",
            "tai42_skeleton.routers.system_kinds",
        ],
        "extensions_modules": _EXTENSION_MODULES,
        "backend_module": variants.backend.module,
        "storage_module": variants.storage.module,
        "tools": [
            _probe_tools_entry(with_backend_branches=True, with_schedule_branch=True),
            _toolbox_tools_entry(),
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    env["ACCESS_CONTROL_ENABLE"] = "true"
    env.update(variants.identity.auth_provider_env())
    env.update(_pg_env("ACCESS_CONTROL_STORE_", res))
    # No agent runs here, but pin the checkpoint/store provider to the in-process
    # ``memory`` one so a settings reload never tries to build a RediSearch index on
    # the plain shared Redis.
    env.update(_memory_agent_state_env())
    return StackConfig(
        name="cli",
        topology=Topology.REPLICAS,
        manifest=manifest,
        env=env,
        run_backend=True,
        run_metrics=True,
        auth=True,
    )


def run_cli(stack: TaiStack, cwd: Path, *args: str, json_flag: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``tai --server <url> [--json] <args...>`` against the booted stack.

    Auth rides ``TAI_API_KEY`` (the stack's seeded root token) — the CLI's env-var
    key path. A clean child env (venv-first PATH, no inherited config) and a neutral
    cwd keep the invocation reproducible; the CLI's own ``load_dotenv`` finds no
    ``.env`` there, so only ``--server`` and the key resolve."""
    assert stack.auth_token is not None, "the CLI stack must be seeded with a root token"
    venv_bin = str(Path(sys.executable).parent)
    env = {
        "PATH": os.pathsep.join([venv_bin, "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": os.environ.get("HOME", str(cwd)),
        "TAI_API_KEY": stack.auth_token,
    }
    server = f"http://{stack.host}:{stack.port_a}"
    argv = [tai_bin(), "--server", server, *(["--json"] if json_flag else []), *args]
    return subprocess.run(argv, env=env, cwd=str(cwd), capture_output=True, text=True, timeout=90)


def cli_json(result: subprocess.CompletedProcess[str]) -> Any:
    """Assert a clean exit and parse the ``--json`` stdout as JSON — the machine
    output contract the spec asserts against (never a bare exit code)."""
    assert result.returncode == 0, f"exit {result.returncode}; stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    return json.loads(result.stdout)
