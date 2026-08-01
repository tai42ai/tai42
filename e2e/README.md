# tai42-e2e

The cross-repo, real-stack, end-to-end functional test suite for the tai /
AgenticOS ecosystem. It boots the REAL deployment topology — a multi-worker
uvicorn skeleton server, a separate backend worker process, a separate metrics
server process, real Redis, real Postgres — and drives it over HTTP.

**Why this suite exists.** Every member suite (tai42-contract, tai42-kit,
tai42-toolbox, tai42-agents, tai42-skeleton) is single-process, in-memory, and
FakeRedis-backed by design, and stays that way. That leaves a whole class of
bug invisible: the ones that live in the seam between processes — env-var
ordering across a spawn boundary, a registry mutated on one worker and read on
another, a socket flipped process-wide, a counter written in one process and
scraped from another. `tai-e2e` is the only suite that can catch them, because
it is the only one that runs the real multi-process topology.

## Quickstart

```bash
docker compose up -d          # core profile: redis:7-alpine + postgres:16
uv python install 3.13
uv sync
uv run pytest                 # the default variant leg: arq + redis identity + local storage
```

The suite runs sequentially in one pytest process (no xdist by design — a stack
is already 5-8 OS processes). A missing Redis/Postgres fails loudly at session
start with the `docker compose up -d` hint. The shared Redis needs no modules —
access control stores its records as plain hashes and counters.

### Variant legs

Every stack is rendered through a plugin VARIANT triple, selected per pytest
process and printed in the run header. An unknown value fails loudly, naming the
valid set.

| Env var | Values | Default |
|---------|--------|---------|
| `TAI_E2E_BACKEND` | `arq` \| `rq` \| `celery` | `arq` |
| `TAI_E2E_IDENTITY` | `redis` \| `fixture` (PG-backed, from `tai42_e2e_fixtures`) | `redis` |
| `TAI_E2E_STORAGE` | `local` \| `fixture` (own on-disk layout) | `local` |

```bash
# the rq and celery backend legs — celery needs its broker
docker compose --profile celery up -d
TAI_E2E_BACKEND=rq     uv run pytest -m "not backendless"
TAI_E2E_BACKEND=celery uv run pytest -m "not backendless"

# the second identity/storage values, on the identity/storage-bearing suites
TAI_E2E_IDENTITY=fixture TAI_E2E_STORAGE=fixture uv run pytest tests/redis_semantics tests/storage tests/harness
```

`backendless` marks a module whose stack runs no backend worker: it exercises no
backend seam, so the non-default backend legs deselect it with
`-m "not backendless"` instead of re-running it three times.

### The checkpoint Redis (agents state)

The langgraph `redis` checkpoint/store provider needs the RediSearch + RedisJSON
modules, which `redis:7-alpine` does not carry. A second, module-capable Redis is
compose-gated behind the `agents-redis` profile and pointed at by
`TAI_E2E_CHECKPOINT_REDIS_URL` (see `.env.example`); the specs that drive it skip
loudly with the compose hint when it is unset, and session start fails loudly if
the URL points at an image without the modules.

```bash
docker compose --profile agents-redis up -d
```

## Seam classes

| # | Bug class | Test dir |
|---|-----------|----------|
| C1 | Multiproc-metrics writer/reader split across processes | `tests/metrics/` |
| C2 | Import-order / module-freeze of `prometheus_client` | `tests/metrics/` |
| C3 | Cross-entrypoint lifecycle on the shared mmap dir | `tests/metrics/` |
| C4 | Cross-worker registry divergence (sub-MCP, presets, tool-extensions) | `tests/crossworker/` |
| C5 | Cross-process lost-update on shared config files | `tests/crossworker/` |
| C6 | Real-Redis / real-Postgres semantics (rate limit, AC, interactions, connectors) | `tests/redis_semantics/`, `tests/interactions/`, `tests/connectors/` |
| C7 | Cross-repo running-service contract (agents, webhooks, tool-runs, storage, monitoring) | `tests/agents/`, `tests/webhooks/`, ... |
| C8 | Process-global socket-state bleed (proxy extension) | `tests/proxy/` |
| C9 | Reload / control-plane fan-out divergence | `tests/reload/` |
| P1 | Plugin-seam switch (backend / identity / storage variants, broker isolation) | `tests/backend/`, `tests/storage/` |
| P2 | Infra failure injection (worker crash, Redis/Postgres outage) | `tests/failures/` |
| P3 | Scheduling across backends (`schedule_task`, the scheduler process) | `tests/scheduling/` |
| P4 | Correctness under width (4 uvicorn workers, ≥32 in-flight) | `tests/scale/` |
| P5 | Tool extensions through the stack (cache, chain, monitor, ask_external, output_schema) | `tests/extensions/` |

## Layout

- `src/tai42_e2e/` — the harness library (imported by tests, never by the SUT):
  boot engine (`stack.py`), process spawning (`procs.py`), Redis/PG/RabbitMQ admin
  (`redisx.py`, `pg.py`, `rabbitx.py`), the plugin-variant adapters (`variants.py`),
  manifest/env profile builders (`manifests.py`), the scripted LLM stub
  (`llmstub.py`), the net fixtures (`netfixtures.py`), the severable TCP relay the
  outage specs inject with (`tcprelay.py`), the single waiting primitive
  (`waiting.py`), and failure diagnostics (`diagnostics.py`).
- `src/tai42_e2e_fixtures/` — SUT-SIDE modules the spawned server imports via its
  manifest: the probe tools (`tools.py`), the fixture OAuth connector provider
  (`connector_provider.py`), and the second value on each pluggable axis — the
  PG-backed identity provider (`identity_provider.py`), the storage backend
  (`storage.py`), and the monitoring backend (`monitor_backend.py`).
- `tests/` — the suites by seam class, plus `tests/harness/` self-tests.
- `docs/adding-a-test.md` — the 6-step recipe for a new feature's e2e test.

## Metrics-dir isolation (the C2 hard rule)

The harness NEVER sets `PROMETHEUS_MULTIPROC_DIR` in a child env — stamping it
is the entrypoint's own job (`activate_multiproc_env`) and is exactly what the
C2 import-order tests verify. Per-stack (and per-replica) metrics dirs are
controlled entirely via `TMPDIR`: `MetricsSettings.prometheus_multiproc_dir`
defaults to `<tempfile.gettempdir()>/tai42_prometheus`, so pointing a process's
`TMPDIR` at a per-run-family dir gives that family its own multiproc dir with
the env var untouched. `stack.py` asserts the rule on every child env.

## xfail policy

A test for a fix that has not yet landed in the workspace is marked
`xfail(strict=True)` with the observed-behavior reason, so it flips loudly
(xpass → failure) the moment the fix lands.

## CI

The fleet e2e runs from the monorepo root workflow, `.github/workflows/ci.yml`,
in two jobs gated by the `changes` filter (any member carrying an e2e trigger, or
a change to a shared root, sets `run_e2e`):

- `e2e` — one matrix leg per triggered backend (`arq`, `rq`, `celery`), under
  `TAI_E2E_FLEET=1` with `redis` identity and `local` storage. Services:
  `redis:7-alpine` (shared), `postgres:16-alpine`, and `redis:8` — the
  module-capable checkpoint Redis the arq leg points
  `TAI_E2E_CHECKPOINT_REDIS_URL` at. The celery leg brings its RabbitMQ up from
  the compose `celery` profile; non-arq legs deselect `-m "not backendless"`. The
  langfuse monitoring profile runs on the arq leg only when the `run_monitoring`
  gate is on (a change under `plugins/monitoring-langfuse`, `core`, or `e2e`, or a
  manual `workflow_dispatch`). When the private-repo token is present it also runs
  the marketplace suite (`TAI_E2E_MARKETPLACE=1`), installing the git-sourced
  `tai42-marketplace` and booting it as harness processes.
- `ui-e2e` — needs `e2e`; boots redis + postgres, clones `tai-studio`, builds the
  Studio SPA, and runs the Playwright chromium suite in `ui/`. When the `run_browsers`
  gate is on (a change under `e2e/ui`, or a manual `workflow_dispatch`) it also
  widens the matrix onto firefox + webkit. With the token it also clones the private
  `tai-marketplace-web` and runs the marketplace browser specs under
  `TAI_E2E_MARKETPLACE=1`.

The marketplace legs need the private `tai42ai/tai-marketplace` and
`tai42ai/tai-marketplace-web` repos, so they run only when the `RELEASE_PLEASE_TOKEN`
secret is available — a fork PR has no secrets, so those steps skip with a visible
`::warning::` rather than failing. Other external consumers install this harness as
the published `tai42-e2e` package and run their own suites against it. Any consumer
booting the studio stack must also install the Studio reference plugin from the
`tai42ai/tai-studio` repo (`e2e/reference-plugin`), which the studio profile manifest
names but the harness does not carry as a runtime dependency.

## Marketplace area (opt-in)

`tests/marketplace/` only collects when `TAI_E2E_MARKETPLACE=1`; unset, it is
skipped at collection like `tests/monitoring/`. When on, the harness installs the
`tai42-marketplace` registry out-of-band from its pinned git source at boot and
boots it — as a harness-managed process (the API server) on an isolated
Postgres database: **no extra compose profile**; it rides the core
`docker compose up -d` infra. The suite forges REAL fixture wheels (and one source
tarball) from the in-repo `fixtures/marketplace_plugins` tree
(`TAI_E2E_MARKETPLACE_FIXTURES` overrides it), served over a local package index,
seeds them through the registry's real ingest pipeline, which publishes each version
synchronously, and the skeleton REALLY pip-installs them into the one shared venv —
so every lifecycle spec ends with its own uninstall and a session guard raises loudly
on any leftover distribution (never auto-cleaning). The ui leg additionally
pnpm-builds and serves the public `tai-marketplace-web` checkout and drives the Studio
marketplace page and the public site over a browser.

```bash
docker compose up -d
TAI_E2E_MARKETPLACE=1 uv run pytest tests/marketplace
# the browser legs (built Studio dist + the tai-marketplace-web checkout):
cd ui && TAI_E2E_MARKETPLACE=1 pnpm exec playwright test tests/marketplace.spec.ts tests/marketplace-web.spec.ts
```
