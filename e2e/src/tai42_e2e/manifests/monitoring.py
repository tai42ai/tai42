"""The monitoring stack profile."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from tai42_e2e.manifests.feature_env import _base_env, _llm_env, _memory_agent_state_env, _switch
from tai42_e2e.manifests.tool_entries import (
    _AGENT_ENTRIES,
    _CORE_ROUTERS,
    _PROJECTED_API_TOOLS,
    _builtin_entries,
    _probe_tools_entry,
)
from tai42_e2e.topology import StackConfig, StackResources, Topology

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants


# The writer's standard OpenTelemetry environment: the processor self-metrics the
# writer counts queue-full drops through, and the service name every record carries.
_OTEL_ENV = {"OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED": "true", "OTEL_SERVICE_NAME": "tai42"}
# An address nothing listens on: every export fails, so the delivery-failure counting
# and the ``monitoring_export_failed`` alert are driven for real.
_DEAD_COLLECTOR_ENV = {
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:9/v1/traces",
    "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT": "2",
    "OTEL_BSP_SCHEDULE_DELAY": "200",
}


def compose_monitoring_resources(environ: Mapping[str, str]) -> dict[str, str]:
    """The compose monitoring profile's coordinates as stack-resource fields: the
    self-hosted Langfuse on ``TAI_E2E_LANGFUSE_PORT`` (default 3000), the headless-init
    key pair the compose file sets, and the collector's OTLP/HTTP traces endpoint on
    ``TAI_E2E_OTEL_HTTP_PORT`` (default 4318) — the ports the compose file publishes."""
    langfuse_port = environ.get("TAI_E2E_LANGFUSE_PORT", "3000")
    otel_http_port = environ.get("TAI_E2E_OTEL_HTTP_PORT", "4318")
    return {
        "langfuse_host": f"http://127.0.0.1:{langfuse_port}",
        "langfuse_public_key": "pk-lf-e2e0000000000000000000000000000",
        "langfuse_secret_key": "sk-lf-e2e0000000000000000000000000000",
        "otel_traces_endpoint": f"http://127.0.0.1:{otel_http_port}/v1/traces",
    }


def build_monitoring_stack(res: StackResources, variants: Variants) -> StackConfig:
    """MULTIWORKER(1) with the langfuse monitoring plugin reading the compose-provided
    self-hosted Langfuse and writing through the compose-provided OpenTelemetry
    collector (opt-in)."""
    manifest = {
        "default_routers": "none",
        "routers_modules": [
            *_CORE_ROUTERS,
            "tai42_skeleton.routers.observability",
            "tai42_skeleton.routers.agents",
            # The runs-index deep-link list: the composed e2e reads a direct/MCP preset
            # run's ``traceId`` back off ``/api/runs`` to prove the row carries the opened
            # trace root's id end to end.
            "tai42_skeleton.routers.runs",
        ],
        # The probe entry attaches a proxy branch and (this profile) an e2e_echo monitor
        # branch, so proxy + prometheus + the monitor builtin must all load or extension
        # validation aborts boot.
        "extensions_modules": [
            "tai42_toolbox.extensions.prometheus",
            "tai42_toolbox.extensions.proxy",
            "tai42_skeleton.extensions.builtin.monitor",
        ],
        "monitoring_module": "tai42_monitoring_langfuse",
        "storage_module": variants.storage.module,
        # e2e_echo_monitor traces each standalone call as a TOOL span, giving the langfuse
        # observability test a real run to read back.
        "tools": [_probe_tools_entry(with_backend_branches=False, with_monitor_branch=True), *_builtin_entries()],
        # The base reference agent, traced natively through the agents plugin's
        # monitoring callbacks, gives the observability test an agent run to read back
        # alongside the tool run.
        "agents": [_AGENT_ENTRIES[0]],
        "api_tools": _PROJECTED_API_TOOLS,
        "user_tools": ["ask", "reload_config"],
    }
    # The langfuse monitoring module is real either way — only the host + key pair
    # change. MOCK: the compose-baked self-hosted coordinates on resources. REAL: the
    # cloud host + key pair from the operator template (``LANGFUSE_*`` read verbatim).
    if _switch().is_real("langfuse"):
        host = os.environ["LANGFUSE_HOST"]
        public_key = os.environ["LANGFUSE_PUBLIC_KEY"]
        secret_key = os.environ["LANGFUSE_SECRET_KEY"]
        traces_endpoint = os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
    else:
        if not (res.langfuse_host and res.langfuse_public_key and res.langfuse_secret_key and res.otel_traces_endpoint):
            # A monitoring stack with blank coordinates boots and then fails cryptically
            # inside the plugin; a missing coordinate is a mis-gated fixture, caught here.
            raise RuntimeError(
                "build_monitoring_stack requires langfuse_host + langfuse_public_key + langfuse_secret_key + "
                "otel_traces_endpoint"
            )
        host, public_key, secret_key = res.langfuse_host, res.langfuse_public_key, res.langfuse_secret_key
        traces_endpoint = res.otel_traces_endpoint
    env = _base_env(res, variants)
    env["LANGFUSE_HOST"] = host
    env["LANGFUSE_PUBLIC_KEY"] = public_key
    env["LANGFUSE_SECRET_KEY"] = secret_key
    env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = traces_endpoint
    env.update(_OTEL_ENV)
    # The reference agent wires a checkpointer and runs on the scripted LLM/embedding
    # stub — pin its checkpoint/store to the in-process memory provider and point its
    # model access at the stub, exactly as the agents stack does for this agent.
    env.update(_memory_agent_state_env())
    env.update(_llm_env(res))
    return StackConfig(
        name="monitoring",
        topology=Topology.MULTIWORKER,
        manifest=manifest,
        env=env,
        workers=1,
        run_backend=False,
        run_metrics=True,
        auth=False,
    )


def build_dead_collector_monitoring_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The monitoring stack exporting to an address nothing listens on (the dead-collector case)."""
    config = build_monitoring_stack(res, variants)
    return dataclasses.replace(config, name="monitoring-dead-collector", env={**config.env, **_DEAD_COLLECTOR_ENV})
