# tai42-monitoring-langfuse

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A Langfuse `Monitoring` backend for the TAI ecosystem. Its **writer** is the kit's
OpenTelemetry writer (`tai42_kit.monitoring.otel.OtelWriter`): every record is
exported OTLP/HTTP to an OpenTelemetry collector, which maps the platform's
attribute names onto Langfuse's and sends them to Langfuse's OTLP endpoint. Its
**reader** (metrics totals, the runtime span window, complete-trace and
single-observation reads) queries a Langfuse server — cloud or self-hosted —
over its public REST API (Observations v2 and Metrics v2) with its own HTTP client.

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
the `tai42_app` handle) and `tai42-kit[monitoring]` (the OpenTelemetry writer,
`TaiBaseSettings` and the settings cache). Beyond those it depends on `httpx`,
`orjson`, and `pydantic` / `pydantic-settings`.

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

Selecting the module but leaving the credentials or the OpenTelemetry export
environment unset **raises at startup** —
a selected backend that cannot build is a loud failure, not a silent downgrade.
To run without monitoring, omit `monitoring_module` entirely (the runtime falls
back to its built-in no-op). A plain `import tai42_monitoring_langfuse` (library
use) does not register anything.

## Configuration

The reader's settings are the `LANGFUSE_` environment group (see
`LangfuseSettings`):

| Env var | Default | Purpose |
| --- | --- | --- |
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse project public key (required) |
| `LANGFUSE_SECRET_KEY` | — | Langfuse project secret key (required) |
| `LANGFUSE_HOST` | — | Langfuse server URL (required) |
| `LANGFUSE_TIMEOUT_SECONDS` | `30` | Read request timeout |
| `LANGFUSE_TRACING_ENVIRONMENT` | `tai` | The `source` marker: stamps every record (`deployment.environment.name`, Langfuse's environment) and scopes every read to it |

These five are the only `LANGFUSE_*` names the backend reads, and the `LANGFUSE_`
prefix is owned: any other `LANGFUSE_*` name (the Langfuse SDK's own
`LANGFUSE_FLUSH_AT`, `LANGFUSE_FLUSH_INTERVAL`, `LANGFUSE_OTEL_TRACES_EXPORT_PATH`,
`LANGFUSE_MEDIA_UPLOAD_ENABLED`, `LANGFUSE_DEBUG`, or a mistyped name) refuses boot
and every env write with an error naming it.

The `source` marker lets several deployments share one Langfuse project while
each reads back only its own data. `get_trace` and `get_observation` are the
unscoped reads — their ids are globally unique.

The writer reads the OpenTelemetry SDK's standard environment: an OTLP endpoint
(`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`) and
`OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED=true` are required; `OTEL_BSP_*`,
`OTEL_SERVICE_NAME` and the other standard names tune it. The collector's Langfuse
credential is `OTEL_COLLECTOR_LANGFUSE_AUTH` (base64 of `public:secret`), outside
this package's `LANGFUSE_` group.

## Run attribution & data retention (operators read this)

The platform attributes a run's trace with generic identity dimensions at its
shared chokepoints: a **user** (`user_id`), a **session** (`session_id`), route
`tags`, and free-form `metadata` (plus a preset's `preset:`/`preset-v:` tags and
a root `version` when a registered preset is dispatched). This writer forwards
them as `user.id` / `session.id` / `tai42.run.version` / `tai42.trace.tags`
attributes, which the collector maps onto Langfuse's native `user_id` /
`session_id` / `version` / `tags` trace dimensions, so a run is filterable and groupable in
Langfuse by exactly those dimensions — including from the `/api/observability/runs`
door's `user` / `session` / `meta.<key>` query params.

Because that identity lands in your Langfuse project, it is **your** data to
govern. Two operator responsibilities:

- **Set a deliberate retention policy.** Langfuse retains traces until you delete
  them; decide a retention window that matches your privacy/compliance posture
  and configure it in Langfuse (project data-retention settings or a scheduled
  deletion job). The platform sets none on your behalf — a trace lives as long as
  your Langfuse project keeps it.
- **Know what `user_id` carries.** It is **person-id-first**: a conversation with
  a resolved (linked or provisional) person is attributed by that person's stable
  `person_id`. When no person exists (the plain, non-multichannel path) it falls
  back to the **raw `{channel}:{client_address}`** the door saw — which, on a
  channel door, is an end-user address (a phone number, a visitor id, an email) —
  or, on the API door where no channel exists, the **bare `client_address`**
  alone. Treat those trace attributes as personal data.
- **Erasure story.** Langfuse deletes by `user_id`: to honour an erasure request,
  delete that subject's traces in Langfuse keyed on the `user_id` above (the
  `person_id`; the raw `{channel}:{address}` for an unlinked channel subject; or
  the bare address for an unlinked API-door subject). Keeping
  `user_id` person-id-first keeps a linked subject's whole cross-channel history
  under one deletable key.

## Behavior notes

- **Writer**: see `tai42_kit.monitoring.otel` — fail-safe emits counted in
  `export_health()`, every `SecretValue` recorded as `[secret]`, values never
  truncated, a forked child counting from zero and rebuilding its own pipeline.
- **Reader**: `async` per the contract; each request on the synchronous `httpx`
  client, and the decoding of its answer, run off the event loop. Every read goes
  through Langfuse's Observations v2 and Metrics v2 REST endpoints
  (`GET /api/public/v2/observations`, `GET /api/public/v2/metrics`) with the key
  pair as HTTP Basic auth. A 408, 429 or 5xx answer is retried up to twice, after
  its `Retry-After` (at most 60 s) or a 1 s / 2 s backoff, each retry logged; any
  other failure — another status, the retries spent, a transport error or
  timeout, an answer that does not decode — raises `MonitoringError` naming the
  request, chained from its cause. A run is one trace with one root
  observation: the run's start is its root's start and its latency is its
  root's duration. `list_traces` returns row SUMMARIES, never trace bodies.
  `get_trace` is the only trace body door; it returns the trace or raises
  `TraceNotFoundError`, and a failed read (e.g. a timeout) raises
  `MonitoringError` — it is never mapped to "not found". `get_observation` reads one
  observation (`TraceNotFoundError` when the trace is absent,
  `ObservationNotFoundError` when the trace holds no such observation); an
  observation's `metadata` is the producer metadata the writer recorded (the
  decoded `tai42.metadata` attribute plus the promoted `tai42.step_role` /
  `tai42.timing`), read whole however long. Each observation's `kind` maps the
  Langfuse type: `GENERATION` / `EMBEDDING` → `LLM`, `TOOL` / `RETRIEVER` →
  `TOOL`, `EVENT` → `EVENT`, every other type → `CHAIN`; its token counts come
  from the usage keys `input` / `output` / `total`.
- **The runs list**: every sort combines with every filter, within these bounds:
  1. Newest first (`timestamp` desc) with only run-level filters (name, user,
     session, version, tags, metadata, the latency range): Langfuse filters and
     orders the runs; paged up to run 5,000.
  2. Cost or tokens, highest first, with only the name and tags filters:
     Langfuse ranks the runs over the whole time range; paged up to run 1,000.
  3. Every other read is ranked by the plugin over the runs whose root started
     in the time range: served when the range holds at most 2,000 matching
     runs, and at most 50,000 observations when a cost, token, status, level or
     model filter or a cost or tokens sort needs every observation of every
     candidate run.

  A read past a bound raises `MonitoringReadTooBroadError` naming the bound;
  bounds 2 and 3 need `from_timestamp`. `export_traces` reads the same list,
  up to its `max_rows`, without the paging bounds of 1 and 2.
- **Step window**: `list_spans_in_window` selects one item per step by the
  producers' `tai42.step_role` marker (a `grouping` or `sub_step` record is never
  an item) and by type (generations, embeddings and events never are).
- **Metrics**: the run count and run latency are read over the runs' roots,
  cost and tokens over every observation, merged on the time bucket; the model
  dimension reads every observation.
- **Declarations**: `max_page_size()` is 100 (a larger `limit` raises
  `ValueError`); `list_capability()` serves the sorts `timestamp`, `name`, `id`,
  `total_cost`, `latency` and `total_tokens`. A sort, measure or dimension
  outside the declarations raises `MonitoringReadNotServedError`.
- **Server**: the Observations v2 and Metrics v2 endpoints need a Langfuse
  server 4.59.0 or newer (cloud, or self-hosted 4.x).

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
they read `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` and the
collector endpoint `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` from the environment and
skip cleanly when unset.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
