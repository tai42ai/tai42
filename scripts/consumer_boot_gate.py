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
    _CORE_TOOLS,
    IdentityFixture,
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
    read_identity_fixture,
)
from _consumer_boot_gate.boot_failure import (
    BootFailure,
    external_service_handlers,
    is_external_service_only,
    parse_boot_failure,
)
from _consumer_boot_gate.candidate import (
    Candidate,
    CoreRelease,
    previous_release_tag,
    read_candidate,
    released_by_tree,
)
from _consumer_boot_gate.cli import _emit_matrix, _load_plugin_yaml, main
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
    _KIT_EXTRAS,
    _install_venv,
    _is_resolution_conflict,
    _report_unresolvable_consumers,
    _ResolutionConflictError,
    _resolver_conclusion,
)
from _consumer_boot_gate.process import run_gate_step
from _consumer_boot_gate.provides import Provides, read_provides
from _consumer_boot_gate.versioning import break_is_accepted

__all__ = [
    "_BOOT_PLACEHOLDER_ENV",
    "_CORE_ROUTERS",
    "_CORE_TOOLS",
    "_KIT_EXTRAS",
    "BootFailure",
    "Candidate",
    "Consumer",
    "CoreRelease",
    "IdentityFixture",
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
    "_resolver_conclusion",
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
    "is_external_service_only",
    "latest_pypi_version",
    "main",
    "parse_boot_failure",
    "previous_release_tag",
    "read_candidate",
    "read_identity_fixture",
    "read_provides",
    "release_bump_set",
    "released_by_tree",
    "run_gate_step",
    "subprocess",
    "wheel_name_version",
]


if __name__ == "__main__":
    main()
