"""The tool-extension stack profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env
from tai42_e2e.manifests.tool_entries import _CORE_ROUTERS, _PROJECTED_API_TOOLS, PROBE_TOOLS_TITLE, _builtin_entries
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

# Extension modules the extensions profile loads so its probe-tool branches resolve —
# startup extension-validation aborts loudly on a referenced-but-unloaded extension.
_TOOL_EXTENSION_MODULES = [
    "tai42_toolbox.extensions.batch",
    "tai42_toolbox.extensions.cache",
    "tai42_toolbox.extensions.chain",
    "tai42_toolbox.extensions.output_schema",
    "tai42_skeleton.extensions.builtin.monitor",
    "tai42_skeleton.extensions.builtin.ask_external",
]


# The JSON Schema the ``output_schema`` extension is author-bound to. ``e2e_worker_info``'s
# result satisfies it; ``e2e_echo``'s (a bare string) cannot — the validate-and-raise half.
_OUTPUT_SCHEMA_CONFIG = {
    "name": "output_schema",
    "config": {
        "schema": {
            "type": "object",
            "properties": {"pid": {"type": "integer"}},
            "required": ["pid"],
        }
    },
}


def build_extensions_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1), no backend — the home of the tool-extension coverage specs
    (cache / chain / output_schema / monitor / ask_external).

    Single worker on purpose: the ``cache`` wrapper's value store is process-local, so a
    two-worker fleet could serve a repeat call from another worker's empty store and mask
    the cache. The fixture monitoring backend records each span the ``monitor`` extension
    opens onto the probe channel; the interactions router + public base URL drive the
    ``ask_external`` flow. The probe tools carry one branch per extension under test."""
    manifest = {
        "default_routers": "none",
        "routers_modules": _CORE_ROUTERS,
        "extensions_modules": _TOOL_EXTENSION_MODULES,
        "monitoring_module": "tai42_e2e_fixtures.monitor_backend",
        "storage_module": variants.storage.module,
        "tools": [
            {
                "title": PROBE_TOOLS_TITLE,
                "module": "tai42_e2e_fixtures.tools",
                "extensions": {
                    # cache: a repeat identical call is served from the store, so
                    # the wrapped tool's record side effect fires only once.
                    "e2e_record": [["cache"]],
                    # chain: transform e2e_echo's output with jq into e2e_record's args;
                    # monitor: trace a standalone call as one TOOL span; output_schema:
                    # echo's string result violates the bound schema (validate-and-raise half).
                    "e2e_echo": [["chain"], ["monitor"], [_OUTPUT_SCHEMA_CONFIG], ["batch"]],
                    # output_schema: worker_info's dict result satisfies the bound
                    # schema, so its branch is the advertise-and-pass half.
                    "e2e_worker_info": [[_OUTPUT_SCHEMA_CONFIG]],
                    # ask_external: drive the human-in-the-loop external ask off the
                    # link the wrapped tool builds from the callback url.
                    "e2e_external_link": [["ask_external"]],
                },
            },
            *_builtin_entries(),
        ],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask_user", "reload_config"],
    }
    env = _base_env(res, variants)
    # ask_external opens an external-format ask_user, which mints a callback ticket from a
    # public base URL (the host is never dialed).
    env["INTERACTIONS_PUBLIC_BASE_URL"] = "https://e2e.local"
    return StackConfig(
        name="extensions",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=False,
        auth=False,
    )
