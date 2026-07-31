# Adding a cross-repo e2e test

A new feature's e2e test is ~30 lines on top of this fixed 6-step pattern. The
design act is step 1; the rest is mechanical.

## 1. Pick the seam class

Which cross-process invariant does the feature touch? Match it to a class in the
README's seam table (C1-C9). If the feature introduces a NEW class of shared
state (a new thing mutated on one worker and read on another), add a row to that
table first — that is the design decision; everything else follows.

## 2. Pick a profile

Reuse an existing profile (`tests/conftest.py`) whenever you can — the boot
count is the speed budget. The profiles:

- `core_stack` — MULTIWORKER(2) + backend + metrics, auth off. Metrics /
  import-order / single-server flows.
- `replicas_stack` — REPLICAS + backend + metrics, auth off. Deterministic A/B
  addressing for every cross-worker and Redis-contention test. Loads the github
  webhook verifier.
- `auth_stack` — REPLICAS with access control ON (the SELECTED identity provider
  + PG policy store) and a seeded bootstrap key.
- `schedule_stack` — REPLICAS + backend + the backend's scheduler process, with the
  `schedule_task` probe branch. The scheduling spec's home, held apart from the
  reload-heavy `replicas` profile (see `build_schedule_stack` for why).
- `agents_stack` — MULTIWORKER(1) + metrics + the scripted LLM stub.
- `agents_redis_stack` — REPLICAS + metrics + the stub, with the langgraph `redis`
  checkpoint/store provider on the module-capable checkpoint Redis. Two replicas,
  so "checkpoint via A, resume via B" is real. Skips when
  `TAI_E2E_CHECKPOINT_REDIS_URL` is unset.
- `extensions_stack` — MULTIWORKER(1), no backend, loading the cache / chain /
  output_schema / monitor / ask_external tool extensions with the fixture
  monitoring backend. One worker, because the `cache` store is process-local.
- `connectors_stack` — REPLICAS + the stub OAuth IdP + random encryption keys.
- `monitoring_stack` — opt-in, the langfuse plugin against the compose stack.
- `marketplace_stack` — opt-in, MULTIWORKER(1) wired at the harness-run
  tai42-marketplace registry (short advisories poll, local package index).
- `bare_stack` — the doors MOUNTED but no storage provider and no backend: the
  honest absent-provider profile (`present: false` / 501).
- `fresh_stack` — a function-scoped factory for tests that mutate global stack
  state (restarts, config races, CWD variants, an injected infra outage). Every
  stack is torn down at test end.

Only add a `build_*_stack` in `manifests.py` when the feature needs a manifest /
env shape no profile has — and add it to `_ALL_BUILDERS` in
`tests/harness/test_stack_lifecycle.py`, which sweeps EVERY profile for the C2
rule.

## 2b. Respect the variant axes

A stack is rendered through a plugin-variant triple (`TAI_E2E_BACKEND` /
`TAI_E2E_IDENTITY` / `TAI_E2E_STORAGE`; see the README). A spec must never
hard-code a plugin's answer — read it from the variant instead:

- the backend's class/module on the `/api/backend` door → `variants.backend.provider_class`
- the storage layout / read-back → `variants.storage.stored_object_path()` / `.read_stored()`
- an on-disk or in-store assertion about identity → `variants.identity`
- a bound on a backend's `sync_task` wait → `variants.backend.task_timeout_env()`

If a spec's stack sets `run_backend=False`, it exercises no backend seam: mark the
module `pytestmark = pytest.mark.backendless` so the rq/celery legs (which run
`-m "not backendless"`) do not re-run it for nothing.

## 3. Add a probe if needed

If the observable is not already visible over the HTTP API, add a deterministic
`@tai42_app.tools.tool` to `src/tai42_e2e_fixtures/tools.py` that exposes it — a
return value or an `e2e_record` Redis side effect. Probes never mock; they
observe what actually happened inside a real process.

## 4. Arrange-act on A, assert on B

Drive the mutation over public HTTP on replica A; observe the effect over public
HTTP on replica B. Use `uniq()` for every resource name (tests share a stack).
Wait for cross-worker propagation with `wait_for_async(..., deadline=...)` — the
ONLY sanctioned waiting primitive; ad-hoc sleeps are banned by the ruff config.
Same-request effects assert immediately. For metrics-bearing features, also
assert the scrape from the separate metrics process. For fleet ops, assert the
confirmed-broadcast result shape (`stack.census()` + the tool result's
`workers` map).

## 5. Assert the negative

Where the bug class is a leak (proxy, rate limit, auth), the sharpest assertion
is "exactly N" and "NOT visible", never just "visible". Order events with an
assert-presence sentinel before asserting absence ("B serves X" before "A
stopped serving Y").

## 6. File it

Put the test under the matching `tests/<class>/` dir. If the underlying fix has
not shipped, mark it `@pytest.mark.xfail(strict=True, reason=...)` describing the
observed wrong behavior, so the flip to green is loud when the fix lands.
