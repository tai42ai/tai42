#!/usr/bin/env python3
"""Consumer boot gate — runnable entrypoint + public facade.

The implementation lives in the private ``_consumer_boot_gate`` package; this
module re-exports its public surface (so ``import consumer_boot_gate`` keeps
resolving every symbol) and is the entrypoint the CI lanes invoke by path. See
``_consumer_boot_gate.cli`` for the full gate description.
"""

from __future__ import annotations

import subprocess  # re-exported so a caller can patch consumer_boot_gate.subprocess.run

from tai42_cli import api_gate  # re-exported so a caller can patch consumer_boot_gate.api_gate

from _consumer_boot_gate.boot import (
    _BOOT_PLACEHOLDER_ENV,
    _CORE_ROUTERS,
    _IDENTITY_LIFECYCLE_MODULE,
    _IDENTITY_PROVIDER_NAME,
    Infra,
    _boot_env,
    _channel_env,
    _db_binding_env,
    _external_service_env,
    _infra_from_args,
    _slot_env,
    auth_providers,
    boot_consumer,
    build_manifest,
)
from _consumer_boot_gate.boot_failure import (
    BootFailure,
    external_service_handlers,
    is_external_service_only,
    parse_boot_failure,
)
from _consumer_boot_gate.cli import _emit_matrix, _load_plugin_yaml, _resolve_version, main
from _consumer_boot_gate.consumers import (
    Consumer,
    _req_dist_name,
    collect_consumers,
    enumerate_first_party,
    first_party_plugin_names,
    latest_pypi_version,
    release_bump_set,
    wheel_name_version,
)
from _consumer_boot_gate.install import (
    _install_venv,
    _is_resolution_conflict,
    _report_unresolvable_consumers,
    _ResolutionConflictError,
)
from _consumer_boot_gate.process import run_gate_step
from _consumer_boot_gate.provides import Provides, read_provides
from _consumer_boot_gate.versioning import break_is_accepted, governing_bump, read_project_version

__all__ = [
    "_BOOT_PLACEHOLDER_ENV",
    "_CORE_ROUTERS",
    "_IDENTITY_LIFECYCLE_MODULE",
    "_IDENTITY_PROVIDER_NAME",
    "BootFailure",
    "Consumer",
    "Infra",
    "Provides",
    "_ResolutionConflictError",
    "_boot_env",
    "_channel_env",
    "_db_binding_env",
    "_emit_matrix",
    "_external_service_env",
    "_infra_from_args",
    "_install_venv",
    "_is_resolution_conflict",
    "_load_plugin_yaml",
    "_report_unresolvable_consumers",
    "_req_dist_name",
    "_resolve_version",
    "_slot_env",
    "api_gate",
    "auth_providers",
    "boot_consumer",
    "break_is_accepted",
    "build_manifest",
    "collect_consumers",
    "enumerate_first_party",
    "external_service_handlers",
    "first_party_plugin_names",
    "governing_bump",
    "is_external_service_only",
    "latest_pypi_version",
    "main",
    "parse_boot_failure",
    "read_project_version",
    "read_provides",
    "release_bump_set",
    "run_gate_step",
    "subprocess",
    "wheel_name_version",
]


if __name__ == "__main__":
    main()
