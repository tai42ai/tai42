# tai42-monitoring-langfuse

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A Langfuse `Monitoring` backend for the TAI ecosystem. It implements both faces
of the `tai42_contract.monitoring` contract: the **writer** (spans, events,
LangChain/LangGraph callback handlers, context propagation, per-project
scoping) and the **reader** (metrics totals, the runtime span window, and
complete-trace reads) over a Langfuse server — cloud or self-hosted.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A
`Monitoring` backend is the runtime's observability provider: the framework
emits tool/hook/agent spans through the registered writer, and the
`/api/observability/*` routes answer dashboards from the same backend's reader.
This package is one such provider (Langfuse); any package can back the same
contract, so this repo is this provider's own full doc home, and the
documentation site covers the platform-level story:

- Observability guide: https://tai42.ai/guides/observe
- Author a monitoring backend (author guide): https://tai42.ai/guides/authors/monitoring-backend
- Ecosystem catalog: https://tai42.ai/reference/catalog

Its only tai-* dependencies are `tai42-contract` (the `Monitoring` /
`MonitoringWriter` / `MonitoringReader` protocols, the neutral data models, and
the `tai42_app` handle) and `tai42-kit` (`TaiBaseSettings` and the settings cache).
Beyond those it depends on the `langfuse` SDK, `langchain`, the OpenTelemetry
API, and `pydantic` / `pydantic-settings`.

## Install

Requires **Python 3.13+**. Install from PyPI into the environment that runs the
server:

```bash
uv add tai42-monitoring-langfuse
```

Or from source — clone this repo and add it as an editable dependency; the
`tai42-*` dependencies resolve in-tree from the workspace.

```bash
git clone https://github.com/tai42ai/tai42   # next to your app checkout
cd /path/to/your/app
uv add --editable ../tai42/plugins/monitoring-langfuse
```

## Discovery

The runtime discovers this backend through the manifest's `monitoring_module`
field: it imports every module under the named package, and the package's
`register` module fires the `@tai42_app.monitoring.register_monitoring` decorator
as a side-effect (there is no entry-point). The decorated zero-arg builder
constructs the backend from the `LANGFUSE_*` environment and installs it as the
process monitoring backend:

```yaml
monitoring_module: tai42_monitoring_langfuse
```

Selecting the module but leaving the credentials unset **raises at startup** —
a selected backend that cannot build is a loud failure, not a silent downgrade.
To run without monitoring, omit `monitoring_module` entirely (the runtime falls
back to its built-in no-op). A plain `import tai42_monitoring_langfuse` (library
use) does not register anything.

## Configuration

Settings are read from the `LANGFUSE_` environment group (see
`LangfuseSettings`):

| Env var | Default | Purpose |
| --- | --- | --- |
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse project public key (required) |
| `LANGFUSE_SECRET_KEY` | — | Langfuse project secret key (required) |
| `LANGFUSE_HOST` | — | Langfuse server URL (required) |
| `LANGFUSE_TIMEOUT_SECONDS` | `30` | SDK client timeout, also passed per read request |
| `LANGFUSE_TRACING_ENVIRONMENT` | `tai` | The `source` marker: stamps every write as the Langfuse `environment` and scopes every read to it |

The `source` marker lets several deployments share one Langfuse project while
each reads back only its own data. `get_trace` is the one unscoped read — a
trace id is globally unique.

## Multi-project scoping

One registered backend can hold several Langfuse projects.
`Monitoring.add_project(ProjectConfig(...))` registers an extra project, and
`writer.scope(public_key)` binds it as the active project for a block — every
emit and read inside the block targets it. Scoping to an unregistered key
raises (a silently mis-scoped block could leak traces across projects).
`writer.disable()` suppresses all emission within a block.

## Behavior notes

- **Writer fail-safety** (contract invariant): the emit methods and the `Span`
  handle catch + log, never raising into application code. `record_span`
  without a `trace_id` is the one raising precondition (a caller bug). The
  lifecycle/propagation methods (`flush`, `shutdown`, `inject_context`,
  `get_monitoring_callbacks`, `scope`, `disable`) propagate errors loudly.
- **Fork safety**: `writer.shutdown()` fully evicts every cached SDK client (not
  just a flush), so a forked child rebuilds a clean client on first use.
- **Reader**: `async` per the contract; the synchronous Langfuse API client is
  dispatched off the event loop. `list_traces` returns row SUMMARIES, never
  trace bodies. For the native (timestamp) sort a page costs one trace-list call
  for the row attributes and previews, one metrics query for the page's token
  totals, and one bounded error-observations query for the page's error status —
  no per-trace body fetch. A metric sort (cost/latency/tokens) adds one ranking
  metrics query and a bounded trace-list walk in place of that single list call.
  `get_trace` is the only body door; it returns the trace or raises
  `TraceNotFoundError`, and a transient failure (e.g. a timeout) propagates
  as-is — it is never mapped to "not found".
- **Metric sorts**: `list_traces` ordered by `total_cost` / `latency` /
  `total_tokens` ranks globally through the Langfuse metrics API (trace.list
  cannot sort on aggregates) and requires `from_timestamp` + `limit`;
  unsupported filter clauses on that path raise `MonitoringReadNotSupportedError`
  naming the offending clauses.
- **Private SDK surface**: the few capabilities the SDK has no public API for
  (project-scope contextvar, full client eviction, explicit-time span emission,
  trace attributes from a span handle) are concentrated in
  `tai42_monitoring_langfuse.sdk_internals`, documented as version-fragile against
  the pinned `langfuse~=4.0.6`.

## Development

```bash
uv venv --python 3.13
uv pip install --no-sources --group dev --editable .
uv run --no-sync pytest --cov --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pyright
```

Live integration tests (`pytest -m integration`) hit a real Langfuse server;
they read `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` from
the environment and skip cleanly when unset.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
