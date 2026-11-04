"""The settings-profile RECYCLE leg.

A profile whose diff carries a RECYCLE-class key rolls a fleet recycle
(``config/service.py::apply_replace_env`` STEP 5 → ``orchestrate_recycle``): each targeted
sibling replies ``applied`` then gracefully self-exits, its supervisor respawns it, and its
replacement rejoins the bus census under the SAME stable slot name at a bumped generation.
Recycle is confirmed on REALITY: each recycled row carries its pre-recycle
``generation_before``, the recycled targets' old lives leave presence, and the response
``fresh`` list counts NEW READY capacity per kind. The applying serve worker's OWN recycle is
a deferred post-response self-exit it reports as ``self-deferred``. The env-write door follows
the same policy: an env write of a recycle-class key rolls the same recycle and answers its
rows under ``recycle`` (the writer self-defers). Two writes are refused upfront, naming the
key with nothing written: one that sets a key the deployment sets (its value wins over a
stored one, so the write could never take effect), on a profile save and an env write alike;
and one that changes a bus-reaching URL, which no recycle can carry.

The harness ``supervised`` mode runs the respawn-on-exit supervisor, so a recycled or
self-exited worker actually comes back. ``BACKEND_MANIFEST_KEY`` is a recycle-class field
(``reload_class = "recycle"``) that the deployment env does not set.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, assert_never

import pytest
from tai42_kit.signals import STOP_SIGNALS

from tai42_e2e import wait_for_async
from tai42_e2e.binaries import tai_bin
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.manifests import build_recycle_stack
from tai42_e2e.readiness import expected_backend_processes
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack
from tai42_e2e.variants import BusWorker
from tai42_e2e.waiting import wait_for

pytestmark = pytest.mark.needs("kind:backend")

# A recycle-class field on the loaded backend (env ``BACKEND_MANIFEST_KEY``) the deployment env
# does not set — so a diff on it rolls a recycle rather than being refused.
_RECYCLE_VAR = "BACKEND_MANIFEST_KEY"


@pytest.fixture
def own_recycle_stack(fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub) -> TaiStack:
    """A recycle stack of the test's own, for a test that rolls the fleet.

    Each worker a roll restarts runs its deployment-keys join in its new life, and that join may
    roll the fleet again after the test has ended, so a stack a test rolled is never handed on.
    """
    return fresh_stack(build_recycle_stack, resource_kwargs={"llm_base_url": llm_stub.base_url})


# The hook-run attempts' shutdown grace of :func:`long_hook_grace_recycle_stack`: longer than the
# roll's step (90 s on the recycle stack) plus a serve worker's 10 s graceful-shutdown cap, so a
# held attempt keeps a serve worker inside its lifespan shutdown past the step the roll gives it.
_LONG_HOOK_GRACE_SECONDS = "120"


@pytest.fixture
def long_hook_grace_recycle_stack(fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub) -> TaiStack:
    """A recycle stack of the test's own whose hook-run attempts get a shutdown grace past the roll's step."""
    return fresh_stack(
        build_recycle_stack,
        env_overrides={"HOOKS_RUN_SHUTDOWN_GRACE_SECONDS": _LONG_HOOK_GRACE_SECONDS},
        resource_kwargs={"llm_base_url": llm_stub.base_url},
    )


def _lives(stack: TaiStack, kind: str) -> dict[str, int]:
    """The live ``{name: generation}`` of a kind on the bus census — the snapshot the
    recycle convergence proof reads (old lives gone + fresh capacity, both keyed on the
    stable slot name and its monotonic generation, never a fresh identity string)."""
    return {worker.name: worker.generation for worker in stack.census() if worker.kind == kind}


# The detail of a recycled row whose target was already leaving under another roll's recycle op
# when this roll reached it. A backend the roll restarts runs its deployment-keys join in its new
# life, and that join rolls every worker still reporting a recycle-class key off its bound value,
# the one this roll is carrying among them, so it may restart a later target of this roll first.
_ALREADY_LEAVING_ON_A_RECYCLE_OP = "already leaving: a fleet recycle op"


def _assert_backend_rows_recycled(rows: list[dict[str, Any]], applied: dict[str, Any]) -> None:
    """Every backend row of a roll converged: ``recycled``, with no detail or the one naming a
    sibling roll's recycle op that reached the target first. A row the roll stopped at
    (``timed-out`` / ``failed``) names why in its detail."""
    for e in rows:
        assert e["status"] == "recycled", f"a backend row did not converge: {e} in {applied}"
        assert e["detail"] in (None, _ALREADY_LEAVING_ON_A_RECYCLE_OP), f"a recycled backend row carries a detail: {e}"


async def _profile_from_stored(stack: TaiStack, changes: dict[str, str], name: str) -> None:
    """Save a profile that is the CURRENT stored env plus ``changes`` — so every stored key
    stays at its current value (zero diff) and the only diff is ``changes``."""
    stored = (await stack.api().get("/api/config/env"))["env"]
    await stack.api().put(f"/api/config/profiles/{name}", json={"env": {**stored, **changes}}, retry_on_reloading=True)


@pytest.mark.timeout(300)
@pytest.mark.needs(
    "mutable",
    "store:redis",
    "process",
    "topology:supervised",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
    "setting:TAI_BUS_APPLY_TIMEOUT=330",
)
async def test_recycle_apply_rolls_the_fleet_and_self_defers_applier(
    own_recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = own_recycle_stack.api()
    before_backend = _lives(own_recycle_stack, "backend")
    before_serve = _lives(own_recycle_stack, "serve")
    assert before_backend, "no backend life before the recycle"
    assert before_serve, "no serve life before the recycle"

    name = uniq("recycle")
    await _profile_from_stored(own_recycle_stack, {_RECYCLE_VAR: uniq("mkey").upper()}, name)

    # The apply blocks through the orchestrated backend recycle (self-exit → respawn →
    # rejoin) before it returns, so give the client room.
    applied = await api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True, timeout=200.0)
    assert applied["refused"] == [], f"a recycle apply on a supervised stack refused a key: {applied}"
    assert applied["recycle_stopped"] is None, f"the recycle roll stopped: {applied}"
    assert applied["awaiting_respawn"] == [], f"a converged apply named a worker awaiting a respawn: {applied}"

    # The recycle report: each recycled backend sibling row carries its pre-recycle
    # generation (never a post-recycle life), and the applying serve worker's own recycle is
    # the deferred self-exit carrying its own name + current generation.
    recycle = applied["recycle"]
    fresh = applied["fresh"]
    backend_rows = [e for e in recycle if e["kind"] == "backend"]
    assert backend_rows, f"backend not recycled: {applied}"
    _assert_backend_rows_recycled(backend_rows, applied)
    for e in backend_rows:
        assert e["name"] in before_backend, f"a recycled backend row named an unknown slot: {e} not in {before_backend}"
        assert e["generation_before"] == before_backend[e["name"]], (
            f"the recycle row's generation_before is not the pre-recycle life: {e} vs {before_backend}"
        )
    self_deferred = [e for e in recycle if e["status"] == "self-deferred"]
    assert len(self_deferred) == 1, f"expected exactly one self-deferred applier row: {applied}"
    applier = self_deferred[0]
    assert applier["kind"] == "serve", f"the applier self-defer row is not a serve worker: {applier}"
    assert applier["name"] in before_serve, f"the applier named an unknown serve slot: {applier} not in {before_serve}"
    assert applier["generation_before"] == before_serve[applier["name"]], (
        f"the applier self-defer row does not carry its own current generation: {applier} vs {before_serve}"
    )

    # Reality — OLD lives gone: each recycled backend target's pre-recycle life has left
    # presence (its name absent, or a higher generation on that name). The orchestrator
    # confirmed this before responding; assert it directly against the census.
    recycled_backends = {e["name"]: e["generation_before"] for e in backend_rows}

    async def old_backend_lives_gone() -> bool:
        live = _lives(own_recycle_stack, "backend")
        return all(name not in live or live[name] > gen for name, gen in recycled_backends.items())

    await wait_for_async(
        old_backend_lives_gone, deadline=90.0, message="a recycled backend's old life never left the census"
    )

    # Reality — counted READY capacity: the response `fresh` list carries a NEW READY
    # backend life covering the recycled count (a census-now fact, never a successor claim).
    fresh_backend = [f for f in fresh if f["kind"] == "backend"]
    assert len(fresh_backend) >= len(backend_rows), (
        f"the fresh backend capacity did not cover the recycled count: fresh={fresh} recycled={backend_rows}"
    )

    # Single-gap deterministic rolling recycle (each backend worker under the respawn
    # supervisor): the fresh backend set IS the same names at generation+1 — a stronger
    # mechanical check that holds only because this scenario reuses each slot's name for its
    # next life.
    fresh_by_name = {f["name"]: f["generation"] for f in fresh_backend}
    for e in backend_rows:
        assert fresh_by_name.get(e["name"]) == e["generation_before"] + 1, (
            f"the recycled backend {e['name']!r} did not reappear at generation+1: fresh={fresh_backend} row={e}"
        )

    # The applier serve worker self-exited (deferred, after the response) and respawned under
    # its stable name at a bumped generation; it serves /health again.
    own_recycle_stack.wait_generation_bump("serve", before_serve, deadline=150.0)

    async def serves_again() -> bool:
        resp = await api.request_raw("GET", "/health")
        return resp.status_code == 200

    await wait_for_async(serves_again, deadline=60.0, message="the respawned applier never served /health again")


# The bound on the drain hold's backend job, which the test releases once it has seen the job's
# backend wait on it.
_DRAIN_HOLD_SECONDS = 200


def _stack_env(stack: TaiStack, process: str) -> dict[str, str]:
    """The env the stack's ``process`` runs with: the stack's env under that process's own."""
    return {**stack.config.env, **stack.config.process_env.get(process, {})}


def _env_seconds(env: dict[str, str], name: str, default: float) -> float:
    """The seconds ``env`` sets ``name`` to, or the setting's ``default``."""
    return float(env.get(name, default))


def _app_shutdown_bounds(env: dict[str, str]) -> float:
    """The app's lifespan-shutdown bounds under ``env``, which run after a worker's runtime stop phase.

    The hook-run attempts twice their shutdown grace, the credential refreshes' close wait, the
    states outbox grace, the tool-run supervisors' drain.
    """
    from tai42_skeleton.connectors.runtime.refresh_owner import ACLOSE_TIMEOUT_SECONDS
    from tai42_skeleton.hooks.settings import HooksSettings
    from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings
    from tai42_skeleton.states.db import StatesSettings

    hook_runs = 2 * _env_seconds(
        env, "HOOKS_RUN_SHUTDOWN_GRACE_SECONDS", HooksSettings.model_fields["run_shutdown_grace_seconds"].default
    )
    states_outbox = _env_seconds(
        env,
        "STATES_OUTBOX_SHUTDOWN_GRACE_SECONDS",
        StatesSettings.model_fields["outbox_shutdown_grace_seconds"].default,
    )
    tool_runs = _env_seconds(
        env, "TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS", ToolRunsSettings.model_fields["shutdown_drain_seconds"].default
    )
    return hook_runs + ACLOSE_TIMEOUT_SECONDS + states_outbox + tool_runs


def _announced_backend_drain(stack: TaiStack) -> float:
    """The drain a leaving ``backend`` worker of ``stack`` announces: its backend's drain bound plus the app's.

    The backend's part is the stack's backend variant's drain figure under the backend
    process's env: the wait that backend lets in-flight work finish within (arq's
    job-completion wait, rq's and celery's task timeout) plus its teardown margin.
    """
    env = _stack_env(stack, "backend")
    return stack.infra.variants.backend.drain_seconds(env) + _app_shutdown_bounds(env)


def _roll_step_seconds(stack: TaiStack) -> float:
    """The step a roll the ``backend`` worker of ``stack`` carries gives each target before its drain extends it.

    The roll's step budget is the app's largest declared drain budget, the tool runs' shutdown drain.
    """
    from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings

    return _env_seconds(
        _stack_env(stack, "backend"),
        "TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS",
        ToolRunsSettings.model_fields["shutdown_drain_seconds"].default,
    )


def _announced_serve_drain(stack: TaiStack) -> float:
    """The drain a leaving ``serve`` worker of ``stack`` announces: its graceful-shutdown cap plus the app's bounds."""
    from tai42_skeleton.settings.settings import AppArgsSettings

    env = _stack_env(stack, "serve")
    cap = _env_seconds(
        env, "APP_ARGS_TIMEOUT_GRACEFUL_SHUTDOWN", AppArgsSettings.model_fields["timeout_graceful_shutdown"].default
    )
    return cap + _app_shutdown_bounds(env)


@pytest.mark.timeout(300)
@pytest.mark.needs(
    "probe-tools",
    "mutable",
    "store:redis",
    "process",
    "topology:supervised",
    "setting:extension:sync_task",
)
async def test_recycle_drains_in_flight_backend_job(own_recycle_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """The in-flight backend job is never lost by the recycle a reload owes, and the roll
    reports the backend ``recycled``. The job holds until the test has seen its backend wait on
    it, and how that wait shows is the backend's declared process model (``job_process``). An
    engine that runs the job in the worker process has the re-importing reload wait the job out
    under the fork gate: the backend worker beside it applies the same reload, while the job's
    worker stamps no ``last_op`` until the job is released, nor stamps it or leaves before
    ``done`` has landed, and the apply's fan-out reports its reload applied. An engine that forks a child per job
    reloads while the child runs; its leaving worker announces the bound of its whole stop on its
    census row — its backend's drain plus the app's shutdown bounds — while its graceful stop
    waits the child out, so the stop is seen waiting on the job whichever backend the roll visits
    first, and ``done`` then lands from that child."""
    api = own_recycle_stack.api()
    key = uniq("drain")
    serve_pids = {worker.pid for worker in own_recycle_stack.census() if worker.kind == "serve"}
    # The settings-queue backend worker runs the job; the queue worker ``b`` serves another queue.
    running_job = _slot_of(own_recycle_stack, "backend")
    job_process = own_recycle_stack.infra.variants.backend.job_process

    # Start a background run of a sync_task-branched probe: the tool-runs supervisor (a serve
    # worker) dispatches it INTO the backend, which RPUSHes a ``started`` marker (its own backend
    # pid), holds until the test releases it, and RPUSHes ``done``.
    probe = {"tool_name": "e2e_drain_hold_sync_task", "arguments": {"key": key, "seconds": _DRAIN_HOLD_SECONDS}}
    await api.post("/api/tool-runs", json=probe, expect=202, retry_on_reloading=True)

    def _values() -> list[str]:
        return [json.loads(rec)["value"] for rec in own_recycle_stack.records(key)]

    wait_for(lambda: "started" in _values(), deadline=30.0, message="the backend job never reported it was in-flight")
    started_pid = json.loads(own_recycle_stack.records(key)[0])["pid"]
    assert started_pid not in serve_pids, f"the job ran on a serve worker ({started_pid}), not a backend"
    assert "done" not in _values(), "the backend job completed before the recycle could fire mid-flight"

    before_backend = _lives(own_recycle_stack, "backend")
    before_serve = _lives(own_recycle_stack, "serve")
    stamps_before = {worker.name: worker.last_op for worker in own_recycle_stack.census()}
    name = uniq("drainrecycle")
    await _profile_from_stored(own_recycle_stack, {_RECYCLE_VAR: uniq("mkey").upper()}, name)

    # The apply blocks through the roll; meanwhile the job's backend is seen waiting on the job.
    applying_from = time.monotonic()
    apply = asyncio.create_task(api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True, timeout=200.0))
    try:
        try:
            if job_process == "child":
                await _see_the_stop_wait_on_the_held_child(own_recycle_stack, running_job, _values)
            elif job_process == "worker":
                await _see_the_reload_wait_on_the_held_job(
                    own_recycle_stack, running_job, stamps_before, applying_from, _values
                )
            else:
                assert_never(job_process)
        finally:
            own_recycle_stack.infra.redis.release(key)
        if job_process == "worker":
            await _see_the_reload_apply_after_the_job(
                own_recycle_stack, running_job, stamps_before, before_backend, started_pid, _values
            )
    finally:
        applied = await apply

    backend_rows = [e for e in applied["recycle"] if e["kind"] == "backend"]
    assert backend_rows, f"backend not recycled: {applied}"
    _assert_backend_rows_recycled(backend_rows, applied)
    if job_process == "worker":
        _assert_reload_applied_on(applied, running_job)

    # The recycle rolled (the backend life bumped to its next generation under its stable name).
    own_recycle_stack.wait_generation_bump("backend", before_backend, deadline=150.0)

    # NOT LOST: the in-flight job ran to completion, so its ``done`` marker lands — and it was
    # written by the SAME (pre-recycle) pid that started it, proving the ORIGINAL job finished
    # rather than a respawned worker re-running it.
    wait_for(
        lambda: "done" in _values(), deadline=90.0, message="the in-flight backend job was LOST (no 'done' marker)"
    )
    done_pids = {json.loads(rec)["pid"] for rec in own_recycle_stack.records(key) if json.loads(rec)["value"] == "done"}
    assert started_pid in done_pids, f"'done' came from a different pid than started ({started_pid} vs {done_pids})"

    # The applier recycles itself after replying: the module's later legs start on its respawned life.
    own_recycle_stack.wait_generation_bump("serve", before_serve, deadline=150.0)

    async def serves_again() -> bool:
        resp = await own_recycle_stack.api().request_raw("GET", "/health")
        return resp.status_code == 200

    await wait_for_async(serves_again, deadline=60.0, message="the respawned applier never served /health again")


async def _see_the_stop_wait_on_the_held_child(
    stack: TaiStack, running_job: str, values: Callable[[], list[str]]
) -> None:
    """See the job's backend leaving while its forked child holds the job, announcing its whole stop's drain.

    The worker reloaded while its child held the job, so its leaving row shows with the job still
    in flight, announcing the bound of its whole stop: its backend's drain plus the app's shutdown
    bounds.
    """

    async def leaving() -> tuple[bool, BusWorker] | None:
        # The records are read before the census, so a row read alongside ``done`` shows the
        # state of the life after the job ended.
        done_seen = "done" in await asyncio.to_thread(values)
        census = await asyncio.to_thread(stack.census)
        row = next((w for w in census if w.name == running_job), None)
        if row is not None and (done_seen or row.state == "recycling"):
            return done_seen, row
        return None

    done_seen, row = await wait_for_async(
        leaving, deadline=150.0, message="the job's backend was never seen leaving during the roll"
    )
    assert not done_seen, f"the held job completed before it was released: {row}"
    assert row.state == "recycling", f"the job's backend was not seen leaving: {row}"
    assert row.drain_seconds == _announced_backend_drain(stack), row


async def _see_the_reload_wait_on_the_held_job(
    stack: TaiStack,
    running_job: str,
    stamps_before: dict[str, dict[str, Any] | None],
    applying_from: float,
    values: Callable[[], list[str]],
) -> None:
    """See the job's worker hold its reload while the job is held in the worker process.

    A worker stamps ``last_op`` into its census row only once an op has applied. The apply's
    reload reaches every backend worker by one broadcast and each rebuilds the same registry, so
    the time the worker beside the job's takes to stamp it, holding no job, is how long a reload
    that does not wait takes; through twice that time from the apply's start, the job's worker
    stamps nothing and is not leaving, and the job has not ended. The reload waits the job out for
    at most ``FORK_QUIESCE_SECONDS`` and then applies anyway, so the whole watch ends inside it
    (the apply's start precedes the reload's).
    """
    from tai42_skeleton.app.reload_gate import FORK_QUIESCE_SECONDS

    siblings = [_slot_of(stack, process) for process in expected_backend_processes(stack) if process != "backend"]
    assert siblings, "the stack runs no backend worker beside the job's to time a reload that does not wait"

    def reloaded(row: BusWorker | None) -> bool:
        return (
            row is not None
            and row.last_op is not None
            and row.last_op != stamps_before.get(row.name)
            and row.last_op["op"] == "reload_config"
        )

    async def job_worker_still_waiting() -> dict[str, BusWorker]:
        # The records are read after the census, so a ``done`` before the row's stamp is seen.
        census = {w.name: w for w in await asyncio.to_thread(stack.census)}
        done_seen = "done" in await asyncio.to_thread(values)
        row = census.get(running_job)
        assert row is not None, "the job's backend life left the census while the job was held"
        assert row.last_op == stamps_before.get(running_job), f"the job's worker applied an op with the job held: {row}"
        assert row.state != "recycling", f"the job's worker began to leave with the job held: {row}"
        assert not done_seen, f"the held job completed before it was released: {row}"
        return census

    async def siblings_reloaded() -> bool:
        census = await job_worker_still_waiting()
        return all(reloaded(census.get(sibling)) for sibling in siblings)

    await wait_for_async(
        siblings_reloaded,
        deadline=FORK_QUIESCE_SECONDS,
        message=f"the backend workers beside the job's ({siblings}) never applied the apply's reload",
    )
    sibling_took = time.monotonic() - applying_from
    watch_until = applying_from + 2 * sibling_took
    assert 2 * sibling_took < FORK_QUIESCE_SECONDS, (
        f"a reload that does not wait took {sibling_took:.1f}s, so twice it passes the {FORK_QUIESCE_SECONDS}s the "
        "reload waits for a job: a wait cannot be told apart from no wait"
    )

    async def watched() -> bool:
        await job_worker_still_waiting()
        return time.monotonic() >= watch_until

    await wait_for_async(
        watched, deadline=FORK_QUIESCE_SECONDS, interval=0.2, message="the watch of the held job never ended"
    )


async def _see_the_reload_apply_after_the_job(
    stack: TaiStack,
    running_job: str,
    stamps_before: dict[str, dict[str, Any] | None],
    before_backend: dict[str, int],
    started_pid: int,
    values: Callable[[], list[str]],
) -> None:
    """See the released job end before the job's worker stamps its reload or its life leaves.

    The worker stamps ``last_op`` right after its reload's reply, and the roll restarts that
    life as soon as the reply reaches it, so the stamp may stand only for a moment: the life
    leaving the census (or coming back at its next generation) ends the watch as the stamp does.
    """

    async def reloaded_or_left() -> BusWorker | bool | None:
        census = await asyncio.to_thread(stack.census)
        row = next((w for w in census if w.name == running_job), None)
        left = row is None or row.generation != before_backend[running_job]
        if not left and row is not None and row.last_op == stamps_before.get(running_job):
            return None
        # The records are read after the census, so a ``done`` written before the stamp is seen.
        done_seen = "done" in await asyncio.to_thread(values)
        assert done_seen, f"the job's worker applied its reload or left before the job ended: {row}"
        return True if left else row

    seen = await wait_for_async(
        reloaded_or_left, deadline=60.0, message="the job's worker never applied its reload once the job was released"
    )
    if isinstance(seen, BusWorker):
        assert seen.last_op is not None, seen
        assert seen.last_op["op"] == "reload_config", seen
        assert seen.pid == started_pid, f"the job's backend life is not the one that started it: {seen}"


def _assert_reload_applied_on(applied: dict[str, Any], running_job: str) -> None:
    """The apply's fan-out reports the reload applied on the job's worker."""
    outcomes = {row["name"]: row["outcome"] for row in applied["fanout"]["results"]}
    assert outcomes.get(running_job) == "applied", f"the job's worker did not apply the reload: {applied['fanout']}"


# A settings key the replicas stack's deployment env sets (the interactions keyspace prefix):
# the deployment's value wins over a stored one, so a write that sets it is refused.
_DEPLOYMENT_VAR = "INTERACTIONS_KEY_PREFIX"
_DEPLOYMENT_REFUSAL = "Refusing an env write setting keys set by the deployment"


@pytest.mark.needs("mutable", f"setting:{_DEPLOYMENT_VAR}")
async def test_a_profile_setting_a_deployment_key_is_refused_at_save(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A profile that sets a key the deployment sets could never apply, so saving it is refused
    at the API, naming the key: the profile is not stored and the stored env is unchanged."""
    api = replicas_stack.api(port=replicas_stack.port_a)
    name = uniq("deploymentkey")
    stored = (await api.get("/api/config/env"))["env"]
    resp = await api.request_raw(
        "PUT", f"/api/config/profiles/{name}", json={"env": {**stored, _DEPLOYMENT_VAR: f"{uniq('prefix')}:"}}
    )
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert _DEPLOYMENT_REFUSAL in error, error
    assert _DEPLOYMENT_VAR in error, error
    got = await api.request_raw("GET", f"/api/config/profiles/{name}")
    assert got.status_code == 404, f"a refused save stored the profile: {got.text}"
    assert (await api.get("/api/config/env"))["env"] == stored, "a refused save changed the stored env"


@pytest.mark.needs("mutable", f"setting:{_DEPLOYMENT_VAR}")
async def test_an_env_write_setting_a_deployment_key_is_refused(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """The env-write door refuses a key the deployment sets UPFRONT, naming it, and stores
    nothing — never an ``applied`` reply for a value the running workers would not take."""
    api = replicas_stack.api(port=replicas_stack.port_a)
    before = (await api.get("/api/config/env"))["env"]
    resp = await api.request_raw("POST", "/api/config/env", json={"env": {_DEPLOYMENT_VAR: f"{uniq('prefix')}:"}})
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert _DEPLOYMENT_REFUSAL in error, error
    assert _DEPLOYMENT_VAR in error, error
    assert (await api.get("/api/config/env"))["env"] == before, "a refused env write changed the stored env"


# A bus-reaching key the deployment env of the recycle stack does not set, so the write reaches
# the bus refusal rather than the deployment refusal.
_BUS_REACHING_VAR = "TAI_DEFAULT_REDIS_URL"


@pytest.mark.timeout(300)
@pytest.mark.needs("mutable", "store:redis", "process", "topology:supervised", f"setting:{_BUS_REACHING_VAR}=unset")
async def test_a_supervised_stack_refuses_a_bus_reaching_env_write(recycle_stack: TaiStack) -> None:
    """A bus-reaching URL reaches the worker bus itself, so no recycle can carry it: the
    env-write door refuses it even where a supervisor would respawn the recycled workers,
    naming the key, and stores nothing."""
    assert _BUS_REACHING_VAR not in recycle_stack.config.env, "the bus refusal needs a key the deployment does not set"
    api = recycle_stack.api()
    before = (await api.get("/api/config/env"))["env"]
    resp = await api.request_raw("POST", "/api/config/env", json={"env": {_BUS_REACHING_VAR: "redis://127.0.0.1:1/0"}})
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert "bus-reaching key(s)" in error, error
    assert _BUS_REACHING_VAR in error, error
    assert (await api.get("/api/config/env"))["env"] == before, "a refused env write changed the stored env"


async def _assert_roll_converged_and_writer_respawns(
    stack: TaiStack, roll: dict, before_backend: dict[str, int], before_serve: dict[str, int]
) -> None:
    """The roll's rows: each backend sibling ``recycled`` from its pre-write life, exactly one
    ``self-deferred`` serve writer row; then the old backend lives leave the census, the writer
    respawns at a bumped generation and serves ``/health`` again."""
    assert roll["recycle_stopped"] is None, f"the recycle roll stopped: {roll}"
    backend_rows = [e for e in roll["recycle"] if e["kind"] == "backend"]
    assert backend_rows, f"backend not recycled: {roll}"
    for e in backend_rows:
        assert e["status"] == "recycled", f"a backend row did not converge: {e} in {roll}"
        assert e["generation_before"] == before_backend[e["name"]], f"not the pre-write life: {e} vs {before_backend}"
    writer = [e for e in roll["recycle"] if e["status"] == "self-deferred"]
    assert len(writer) == 1, f"expected exactly one self-deferred writer row: {roll}"
    assert writer[0]["kind"] == "serve", writer
    assert writer[0]["generation_before"] == before_serve[writer[0]["name"]], writer

    recycled = {e["name"]: e["generation_before"] for e in backend_rows}

    async def old_backend_lives_gone() -> bool:
        live = _lives(stack, "backend")
        return all(name not in live or live[name] > gen for name, gen in recycled.items())

    await wait_for_async(old_backend_lives_gone, deadline=90.0, message="a recycled backend's old life never left")
    stack.wait_generation_bump("serve", before_serve, deadline=150.0)

    async def serves_again() -> bool:
        resp = await stack.api().request_raw("GET", "/health")
        return resp.status_code == 200

    await wait_for_async(serves_again, deadline=60.0, message="the respawned writer never served /health again")


@pytest.mark.timeout(420)
@pytest.mark.needs(
    "mutable",
    "store:redis",
    "process",
    "topology:supervised",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
    "setting:TAI_BUS_APPLY_TIMEOUT=330",
)
async def test_an_env_write_of_a_recycle_class_key_rolls_the_fleet_and_self_defers_the_writer(
    own_recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """``POST /api/config/env`` of a recycle-class key on a supervised stack rolls the same
    recycle a profile apply rolls and answers its rows under ``recycle``."""
    api = own_recycle_stack.api()
    before_backend = _lives(own_recycle_stack, "backend")
    before_serve = _lives(own_recycle_stack, "serve")

    written = await api.post(
        "/api/config/env", json={"env": {_RECYCLE_VAR: uniq("mkey").upper()}}, retry_on_reloading=True, timeout=200.0
    )
    assert written["fanout"]["mode"] in ("fleet", "local-only"), written
    # The operation's verdict: a converged roll with the writer recycling itself leaves nobody.
    assert written["awaiting_respawn"] == [], written
    await _assert_roll_converged_and_writer_respawns(
        own_recycle_stack, written["recycle"], before_backend, before_serve
    )


@pytest.mark.timeout(420)
@pytest.mark.needs(
    "mutable",
    "store:redis",
    "process",
    "cli",
    "topology:supervised",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
    "setting:TAI_BUS_APPLY_TIMEOUT=330",
)
async def test_tai_config_env_set_of_a_recycle_class_key_prints_the_rows_of_its_roll(
    own_recycle_stack: TaiStack, uniq: Callable[[str], str], tmp_path: Path
) -> None:
    """``tai config env set`` of a recycle-class key over the CLI rolls the same recycle the HTTP
    door rolls and prints its rows under ``recycle``."""
    before_backend = _lives(own_recycle_stack, "backend")
    before_serve = _lives(own_recycle_stack, "serve")
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": str(tmp_path),
        "TAI_SERVER_URL": own_recycle_stack.origin(own_recycle_stack.port_a),
        "TAI_API_KEY": "sk-e2e-cli-not-a-real-key",
    }
    proc = subprocess.run(
        [tai_bin(), "--json", "config", "env", "set", f"{_RECYCLE_VAR}={uniq('mkey').upper()}"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    assert proc.returncode == 0, f"tai config env set failed: {proc.stdout}\n{proc.stderr}"
    printed = json.loads(proc.stdout)
    assert printed["awaiting_respawn"] == [], printed
    await _assert_roll_converged_and_writer_respawns(
        own_recycle_stack, printed["recycle"], before_backend, before_serve
    )


_SUPERVISED_RECYCLE_NEEDS = (
    "mutable",
    "store:redis",
    "process",
    "topology:supervised",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
    "setting:TAI_BUS_APPLY_TIMEOUT=330",
)


@pytest.mark.timeout(480)
@pytest.mark.needs(*_SUPERVISED_RECYCLE_NEEDS)
async def test_an_env_write_over_mcp_rolls_the_fleet_and_the_writer_recycles_itself_after_the_call(
    own_recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """The projected ``write_env`` tool (MCP ``tools/call``) of a recycle-class key rolls the same
    recycle the HTTP door rolls, answers the writer's ``self-deferred`` row, and the writer then
    exits gracefully once the call returned: its supervisor respawns it at a bumped generation."""
    before_backend = _lives(own_recycle_stack, "backend")
    before_serve = _lives(own_recycle_stack, "serve")

    async with own_recycle_stack.mcp(auth=own_recycle_stack.auth_token) as mcp:
        # The write's reload retires this session and the writer then exits: the tool listing
        # is read first so the client asks this session nothing after the call answered.
        await mcp.list_tools()
        result = await mcp.call_tool(
            "write_env", {"env": {_RECYCLE_VAR: uniq("mkey").upper()}}, retry_on_reloading=True, ready_deadline=30.0
        )
    written = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(written, dict), f"write_env returned no result map: {result!r}"
    assert written["awaiting_respawn"] == [], written
    await _assert_roll_converged_and_writer_respawns(
        own_recycle_stack, written["recycle"], before_backend, before_serve
    )


@pytest.mark.timeout(420)
@pytest.mark.needs(*_SUPERVISED_RECYCLE_NEEDS)
async def test_a_backup_env_restore_of_a_recycle_class_key_rolls_the_fleet_and_self_defers_the_writer(
    own_recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """``POST /api/backup/import`` of an ``env`` section carrying a recycle-class key is an env
    write: the section's report carries the roll under ``recycle`` and an empty verdict, and the
    writer recycles itself after replying."""
    api = own_recycle_stack.api()
    before_backend = _lives(own_recycle_stack, "backend")
    before_serve = _lives(own_recycle_stack, "serve")

    document = {
        "version": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "sections": {"env": {_RECYCLE_VAR: uniq("mkey").upper()}},
    }
    imported = await api.post(
        "/api/backup/import",
        json={"document": document, "sections": ["env"], "mode": "overwrite"},
        retry_on_reloading=True,
        timeout=200.0,
    )
    assert imported["ok"] is True, imported
    section = imported["sections"]["env"]
    assert section["errors"] == [], section
    assert section["awaiting_respawn"] == [], section
    await _assert_roll_converged_and_writer_respawns(
        own_recycle_stack, section["recycle"], before_backend, before_serve
    )


# The lines a converged env write never logs, on any worker: the operation's verdict naming a
# worker nothing respawns, a fleet reload a worker did not confirm, a writer exit that failed, a
# second shutdown signal escalating a worker's warm drain to a cold stop, and the job that cold
# stop cancelled being queued to run again.
_FORBIDDEN_LINES = (
    "worker(s) awaiting a respawn",
    "did not fully converge",
    "post-reply task failed",
    "cold shutdown requested",
    "cancelled, will be run again",
    "did not converge",
    "cancelled mid-roll",
)

# The line a backend worker logs when the one shutdown signal of its life reaches it and it
# starts its warm drain.
_WARM_SHUTDOWN_LINE = "warm shutdown requested (SIGTERM)"

# The line a worker logs when it sends itself the one shutdown signal of its life; the reason
# the writer of a change leaves for.
_LEAVES_LINE = "this worker leaves gracefully"
# The line the backend worker logs when it starts the roll a job it ran handed to it.
_CARRIED_LINE = "handed to this worker"
_WRITER_EXIT_REASON = "the writer's own restart after its change's roll"


def _log_offsets(stack: TaiStack) -> dict[str, int]:
    """The current length of the serve and every backend worker's log, so a leg reads only what it caused."""
    return {name: len(_read_log(stack, name)) for name in ("serve", *expected_backend_processes(stack))}


def _read_log(stack: TaiStack, name: str) -> str:
    return stack.process(name).log_path.read_text(encoding="utf-8", errors="replace")


def _assert_no_forbidden_lines(stack: TaiStack, offsets: dict[str, int], *, expected: tuple[str, ...] = ()) -> None:
    """No log line since ``offsets`` carries a forbidden marker, except a line that carries one of
    the ``expected`` texts: a failure the leg itself causes and asserts on its own."""
    for name, offset in offsets.items():
        text = _read_log(stack, name)
        for line in text[offset:].splitlines():
            if any(known in line for known in expected):
                continue
            assert not any(marker in line for marker in _FORBIDDEN_LINES), f"{name} logged: {line}"


def _assert_the_writer_left_once_for_its_own_change(stack: TaiStack, offsets: dict[str, int]) -> None:
    """The serve writer left once, for its own change, however the backend that joined during its
    roll reached it.

    The joiner's roll asks the writer to restart when it reads the writer's census row ready: a
    recycle op that lands while the writer's roll still runs is answered that its exit is deferred
    to its own change, and one that lands after the writer armed that exit is answered that it is
    already leaving for it. A joiner that reads the writer's row already ``recycling`` waits for the
    writer's next life instead, which runs on the new value and needs no restart. Which of these
    happens is a race between the two processes; in each one the writer leaves only for its own
    change."""
    lines = _read_log(stack, "serve")[offsets["serve"] :].splitlines()
    leaves = [line for line in lines if _LEAVES_LINE in line]
    assert len(leaves) == 1, f"expected the serve writer to leave once: {leaves}"
    assert _WRITER_EXIT_REASON in leaves[0], f"the serve writer left for another reason: {leaves[0]}"


def _assert_each_backend_worker_drained_once(stack: TaiStack, offsets: dict[str, int]) -> None:
    """Each backend worker's log since ``offsets`` carries exactly one warm-shutdown line: each
    worker's life departed once, signalled once, however many doors asked it to leave."""
    for name in expected_backend_processes(stack):
        lines = [line for line in _read_log(stack, name)[offsets[name] :].splitlines() if _WARM_SHUTDOWN_LINE in line]
        assert len(lines) == 1, f"expected one warm shutdown line in the {name} log: {lines}"


def _slot_of(stack: TaiStack, process: str) -> str:
    """The bus census slot name of the harness process ``process``, found by its pid."""
    pid = stack.process(process).pid
    rows = [worker for worker in stack.census() if worker.pid == pid]
    assert len(rows) == 1, f"expected one census row for {process} (pid {pid}): {stack.census()}"
    return rows[0].name


def _tool_map(result: Any) -> dict[str, Any]:
    """The map a tool call returned, from the deserialized ``data`` or the ``{"result": …}`` envelope."""
    if isinstance(result.data, dict):
        return result.data
    content = result.structured_content
    if isinstance(content, dict) and isinstance(content.get("result"), dict):
        return content["result"]
    if isinstance(content, dict):
        return content
    raise AssertionError(f"the tool call returned no map: {result!r}")


@pytest.mark.timeout(480)
@pytest.mark.needs(*_SUPERVISED_RECYCLE_NEEDS, "probe-tools", "setting:extension:async_task")
async def test_an_env_write_run_as_a_backend_job_recycles_the_backend_worker_that_ran_it(
    own_recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """``write_env`` queued as a backend job (``run_tool_async_task``, the queue a schedule fires
    through) is written by the backend worker that ran the job, under the job's budget: the job
    answers with the roll handed to that worker, which then recycles the serve worker and the
    queue worker ``b``, and restarts itself last."""
    before_backend = _lives(own_recycle_stack, "backend")
    before_serve = _lives(own_recycle_stack, "serve")
    writer = _slot_of(own_recycle_stack, "backend")
    offsets = _log_offsets(own_recycle_stack)

    task_id = await _queue_as_backend_job(own_recycle_stack, "write_env", {"env": {_RECYCLE_VAR: uniq("mkey").upper()}})

    # The carried roll recycles the serve worker; the respawned serve reads the job's recorded result.
    own_recycle_stack.wait_generation_bump("serve", before_serve, deadline=200.0)
    written = _tool_map(await _backend_job_result(own_recycle_stack, task_id))

    assert written["recycle"] is None, written
    _assert_every_worker_applied(written["fanout"])
    await _assert_the_backend_worker_carries_the_roll(
        own_recycle_stack, written, writer, {**before_backend, **before_serve}, offsets
    )


@pytest.mark.timeout(480)
@pytest.mark.needs(*_SUPERVISED_RECYCLE_NEEDS, "probe-tools", "setting:extension:sync_task")
async def test_an_env_write_run_as_a_sync_task_hands_the_roll_to_the_backend_worker_that_ran_it(
    own_recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """``write_env`` run inside a backend worker through ``run_tool_sync_task`` (the caller waits
    on the job's result) answers the caller with the roll handed to the backend worker that ran
    the job, which then recycles the serve worker and the queue worker ``b`` and restarts itself
    last."""
    before_backend = _lives(own_recycle_stack, "backend")
    before_serve = _lives(own_recycle_stack, "serve")
    writer = _slot_of(own_recycle_stack, "backend")
    offsets = _log_offsets(own_recycle_stack)

    async with own_recycle_stack.mcp(auth=own_recycle_stack.auth_token) as mcp:
        written = _tool_map(
            await mcp.call_tool(
                "run_tool_sync_task",
                {"tool_name": "write_env", "arguments": {"env": {_RECYCLE_VAR: uniq("mkey").upper()}}},
                retry_on_reloading=True,
            )
        )

    assert written["recycle"] is None, written
    _assert_every_worker_applied(written["fanout"])
    own_recycle_stack.wait_generation_bump("serve", before_serve, deadline=200.0)
    await _assert_the_backend_worker_carries_the_roll(
        own_recycle_stack, written, writer, {**before_backend, **before_serve}, offsets
    )


def _assert_every_worker_applied(fanout: dict[str, Any]) -> None:
    """The reload fan-out a backend job published reached the whole fleet, every worker ``applied`` —
    the worker running the job included, whose pool turnover does not wait for that job."""
    assert fanout["mode"] == "fleet", fanout
    assert fanout["results"], fanout
    for row in fanout["results"]:
        assert row["outcome"] == "applied", f"{row} in {fanout}"


async def _assert_the_backend_worker_carries_the_roll(
    stack: TaiStack,
    reply: dict[str, Any],
    writer: str,
    before: dict[str, int],
    offsets: dict[str, int],
) -> None:
    """A backend job's change answers with its roll handed to ``writer``, the backend worker that
    ran the job, and nobody awaiting a respawn; that worker then carries the roll out: every other
    worker in ``before`` (its ``{slot: generation}`` before the change) comes back at its next
    life, ``writer`` restarts last — its new life joins the census after every other worker's
    new life — and comes back too, each backend worker was signalled once, the serve answers
    ``/health``, and no worker logged a forbidden line."""
    assert reply["roll_deferred"] is True, reply
    assert reply["roll_deferred_to"] == writer, reply
    assert reply["awaiting_respawn"] == [], reply

    others = {name: generation for name, generation in before.items() if name != writer}
    for name, generation in others.items():
        stack.wait_generation_bump(name, generation, deadline=200.0)
    stack.wait_generation_bump(writer, before[writer], deadline=200.0)
    _assert_the_writer_joined_last(stack, writer, before)

    async def serves_again() -> bool:
        resp = await stack.api().request_raw("GET", "/health")
        return resp.status_code == 200

    await wait_for_async(serves_again, deadline=60.0, message="the respawned serve never served /health again")
    carried = _read_log(stack, "backend")[offsets["backend"] :]
    assert _CARRIED_LINE in carried, f"the backend worker that ran the job never carried the roll: {carried[-2000:]}"
    _assert_no_forbidden_lines(stack, offsets)
    _assert_each_backend_worker_drained_once(stack, offsets)


def _assert_the_writer_joined_last(stack: TaiStack, writer: str, before: dict[str, int]) -> None:
    """``writer``'s new life joined the census after the new life of every other worker in
    ``before``: the writer departs only once its roll has brought each of them back, so each of
    their new lives joins first."""
    rows = {worker.name: worker for worker in stack.census()}
    writer_row = rows[writer]
    for name, generation in before.items():
        if name == writer:
            continue
        row = rows.get(name)
        assert row is not None, f"{name} has no census row once the writer {writer} came back: {list(rows.values())}"
        assert row.generation == generation + 1, f"{name} restarted more than once: {row} (before: {generation})"
        assert datetime.fromisoformat(row.joined_at) < datetime.fromisoformat(writer_row.joined_at), (
            f"the writer {writer} did not restart last: {writer_row} joined before {row}"
        )


async def _queue_as_backend_job(stack: TaiStack, tool_name: str, arguments: dict[str, Any]) -> str:
    """Queue ``tool_name`` as a backend job (``run_tool_async_task``) and return its task id."""
    async with stack.mcp(auth=stack.auth_token) as mcp:
        queued = _tool_map(
            await mcp.call_tool(
                "run_tool_async_task", {"tool_name": tool_name, "arguments": arguments}, retry_on_reloading=True
            )
        )
    return str(queued["task_id"])


async def _backend_job_result(stack: TaiStack, task_id: str) -> Any:
    """The backend job's recorded outcome, waited for: its result map, or the error result it failed with."""
    async with stack.mcp(auth=stack.auth_token) as mcp:
        return await mcp.call_tool(
            "backend_task_result",
            {"task_id": task_id, "timeout": 120},
            raise_on_error=False,
            retry_on_reloading=True,
            ready_deadline=60.0,
        )


@pytest.mark.timeout(480)
@pytest.mark.needs(*_SUPERVISED_RECYCLE_NEEDS, "probe-tools", "setting:extension:async_task")
async def test_a_profile_apply_run_as_a_backend_job_recycles_the_fleet_and_the_backend_worker_that_ran_it(
    own_recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """``apply_profile`` queued as a backend job: the job proves the profile builds, persists it
    and answers with the roll handed to the backend worker that ran it, which then recycles the
    serve worker and the queue worker ``b`` and restarts itself last."""
    before_backend = _lives(own_recycle_stack, "backend")
    before_serve = _lives(own_recycle_stack, "serve")
    writer = _slot_of(own_recycle_stack, "backend")
    offsets = _log_offsets(own_recycle_stack)
    name = uniq("jobprofile")
    await _profile_from_stored(own_recycle_stack, {_RECYCLE_VAR: uniq("mkey").upper()}, name)

    task_id = await _queue_as_backend_job(own_recycle_stack, "apply_profile", {"name": name})
    own_recycle_stack.wait_generation_bump("serve", before_serve, deadline=200.0)
    applied = _tool_map(await _backend_job_result(own_recycle_stack, task_id))

    assert applied["refused"] == [], applied
    assert (applied["recycle"], applied["recycle_stopped"]) == ([], None), applied
    _assert_every_worker_applied(applied["fanout"])
    await _assert_the_backend_worker_carries_the_roll(
        own_recycle_stack, applied, writer, {**before_backend, **before_serve}, offsets
    )


# How long past the roll's step the held serve worker is kept leaving once it is first seen so.
# The roll's step for it began before that first sight, so its unextended deadline has passed.
_PAST_THE_STEP_SECONDS = 5.0


def _held_by(stack: TaiStack, key: str) -> list[int]:
    """The pids of the processes that started the hold ``key``."""
    return [json.loads(rec)["pid"] for rec in stack.records(key)]


async def _leaving_row(stack: TaiStack, slot: str) -> BusWorker | None:
    """``slot``'s census row when it reads ``recycling``, else ``None``."""
    census = await asyncio.to_thread(stack.census)
    return next((w for w in census if w.name == slot and w.state == "recycling"), None)


async def _settings_epoch(stack: TaiStack) -> int:
    async with stack.mcp(auth=stack.auth_token) as mcp:
        snapshot = _tool_map(await mcp.call_tool("e2e_settings_snapshot", {}, retry_on_reloading=True))
    return int(snapshot["settings_epoch"])


async def _hold_the_queue_worker(stack: TaiStack, key: str) -> None:
    """Start a job on the queue worker ``b`` that holds until ``key`` is released, and wait until it holds.

    A job holding ``b`` keeps a roll from reaching the serve worker: the roll restarts the backend
    workers before the serve worker, and that worker's turn waits on the held job, whether the job
    holds back its reload (an engine that runs jobs in-process) or its drain (an engine that forks a
    child per job).
    """
    async with stack.mcp(auth=stack.auth_token) as mcp:
        await mcp.call_tool(
            "run_tool_async_task",
            {"tool_name": "e2e_hold_until_released", "arguments": {"key": key}, "queue": "b"},
            retry_on_reloading=True,
        )
    wait_for(lambda: _held_by(stack, key) != [], deadline=60.0, message="the queue worker never started its hold")


async def _apply_a_profile_as_a_backend_job_until_serve_reloads(stack: TaiStack, uniq: Callable[[str], str]) -> str:
    """Queue ``apply_profile`` of a fresh recycle-class profile as a backend job and return its task id
    once the serve worker has applied the reload.

    The change hands its roll to the backend worker that ran the job, which restarts the queue
    worker ``b`` and then the serve worker.
    """
    name = uniq("rollprofile")
    await _profile_from_stored(stack, {_RECYCLE_VAR: uniq("mkey").upper()}, name)
    before_epoch = await _settings_epoch(stack)
    task_id = await _queue_as_backend_job(stack, "apply_profile", {"name": name})

    async def serve_reloaded() -> bool:
        return await _settings_epoch(stack) > before_epoch

    await wait_for_async(serve_reloaded, deadline=60.0, message="the serve worker never applied the reload")
    return task_id


@pytest.mark.timeout(600)
@pytest.mark.needs(
    *_SUPERVISED_RECYCLE_NEEDS,
    "probe-tools",
    "setting:extension:async_task",
    f"setting:HOOKS_RUN_SHUTDOWN_GRACE_SECONDS={_LONG_HOOK_GRACE_SECONDS}",
)
async def test_a_serve_worker_held_in_its_lifespan_shutdown_keeps_announcing_its_drain(
    long_hook_grace_recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A serve worker the roll restarts while a hook-run attempt holds it inside its lifespan
    shutdown, longer than the roll's step, keeps its census row through that stop: past its
    server's stop phase and past the step the row still reads ``recycling``, fresh, announcing its
    stop's drain. The roll's wait for it is extended by that drain, so once the attempt ends the
    process exits, the row is gone, the slot carries the next life, and the roll converges."""
    stack = long_hook_grace_recycle_stack
    api = stack.api()
    [serve] = [worker for worker in stack.census() if worker.kind == "serve"]
    before_backend = _lives(stack, "backend")
    writer = _slot_of(stack, "backend")
    offsets = _log_offsets(stack)
    queue_hold, hook_hold = uniq("queuehold"), uniq("hookhold")

    try:
        try:
            # The held queue worker keeps the roll from the serve worker, so the serve worker, its
            # reload applied, still serves until the job is released.
            await _hold_the_queue_worker(stack, queue_hold)
            task_id = await _apply_a_profile_as_a_backend_job_until_serve_reloads(stack, uniq)

            # A hook-run attempt started on the serve worker's reloaded generation holds until
            # released: its shutdown stop gives the attempt its grace before cancelling it, so the
            # worker stays inside that lifespan step.
            topic = uniq("topic").replace("_", "-")
            await api.post(
                "/api/hooks",
                json={
                    "name": uniq("hook"),
                    "topic": topic,
                    "tool": "e2e_hold_until_released",
                    "tool_kwargs": {"key": hook_hold},
                    "execution_key": uniq("exec"),
                },
                retry_on_reloading=True,
            )
            fired = await api.request_raw("POST", f"/universal_webhook/{topic}", json={"any": "payload"})
            assert fired.status_code == 200, fired.text
            wait_for(
                lambda: _held_by(stack, hook_hold) != [],
                deadline=30.0,
                message="the hook-run attempt never started its hold",
            )
            assert _held_by(stack, hook_hold) == [serve.pid], (
                f"the hold ran outside the serve worker {serve}: {_held_by(stack, hook_hold)}"
            )
        finally:
            # The queue worker's job ends, so the roll moves on to the serve worker.
            stack.infra.redis.release(queue_hold)

        first = await wait_for_async(
            lambda: _leaving_row(stack, serve.name),
            deadline=200.0,
            message="the serve worker was never seen leaving on the census",
        )
        # Past the serve worker's graceful-shutdown cap and past the roll's step: the stop phase
        # and the unextended step the row must outlive.
        await asyncio.sleep(_roll_step_seconds(stack) + _PAST_THE_STEP_SECONDS)  # noqa: TID251 — the hold the row outlives
        held = await _leaving_row(stack, serve.name)
    finally:
        stack.infra.redis.release(hook_hold)

    # Past its server's stop phase and the roll's step, inside its lifespan shutdown, the leaving
    # life still announces its drain.
    assert held is not None, f"the serve worker's row was gone inside its lifespan shutdown (first seen: {first})"
    assert (held.generation, held.pid) == (serve.generation, serve.pid), held
    assert held.beat_at > first.beat_at, f"the leaving row was not kept fresh: {first} -> {held}"
    assert held.drain_seconds == _announced_serve_drain(stack), held

    # The attempt ended, the process exited, and its next life took the slot.
    stack.wait_generation_bump("serve", {serve.name: serve.generation}, deadline=200.0)
    assert not [w for w in stack.census() if w.name == serve.name and w.pid == serve.pid]
    # The hold outlasted the roll's step, and the roll still converges: the drain the leaving row
    # announced extended the roll's wait for it.
    applied = _tool_map(await _backend_job_result(stack, task_id))
    assert applied["refused"] == [], applied
    await _assert_the_backend_worker_carries_the_roll(
        stack, applied, writer, {**before_backend, serve.name: serve.generation}, offsets
    )


def _stop_from_outside(stack: TaiStack, process: str, sig: signal.Signals) -> None:
    """Send the stack's ``process`` the one stop signal ``sig`` of a stop decided outside it, to
    that process alone, as a container runtime (``SIGTERM``) or a terminal's interrupt sent to the
    worker (``SIGINT``) does: docker and Kubernetes signal the container's PID 1, never its process
    group."""
    os.kill(stack.process(process).pid, sig)


# How long the probe job runs on the backend worker a stop from outside reaches: past the read of
# its leaving row and the start of the roll published while it drains.
_OUTSIDE_STOP_PROBE_SECONDS = 30
# How soon a stopped worker's census row reads ``recycling``: well inside its drain.
_LEAVING_SEEN_SECONDS = 8.0


@pytest.mark.timeout(480)
@pytest.mark.needs(*_SUPERVISED_RECYCLE_NEEDS, "probe-tools", "setting:extension:sync_task")
@pytest.mark.parametrize("sig", STOP_SIGNALS, ids=lambda sig: sig.name)
async def test_a_backend_worker_stopped_from_outside_mid_job_reads_leaving_and_a_roll_finishes_its_drain(
    own_recycle_stack: TaiStack, uniq: Callable[[str], str], sig: signal.Signals
) -> None:
    """A backend worker whose stop is decided outside it (a deploy, a scale-down, an interrupt)
    while it runs a job reads ``recycling`` on the census from that stop's first signal,
    ``SIGTERM`` or ``SIGINT`` alike, announcing its whole stop's drain. A roll published while it
    drains finds it leaving and waits its drain out: the job runs to its end once, on the life that
    started it, no worker is signalled a second time into a cold stop, and the roll converges with
    the worker's next life."""
    stack = own_recycle_stack
    api = stack.api()
    key = uniq("outsidestop")
    running_job = _slot_of(stack, "backend")
    offsets = _log_offsets(stack)

    await api.post(
        "/api/tool-runs",
        json={
            "tool_name": "e2e_drain_probe_sync_task",
            "arguments": {"key": key, "seconds": _OUTSIDE_STOP_PROBE_SECONDS},
        },
        expect=202,
        retry_on_reloading=True,
    )

    def _values() -> list[str]:
        return [json.loads(rec)["value"] for rec in stack.records(key)]

    wait_for(lambda: "started" in _values(), deadline=30.0, message="the backend job never reported it was in-flight")
    started_pid = json.loads(stack.records(key)[0])["pid"]
    before_backend = _lives(stack, "backend")
    before_serve = _lives(stack, "serve")

    _stop_from_outside(stack, "backend", sig)
    row = await wait_for_async(
        lambda: _leaving_row(stack, running_job),
        deadline=_LEAVING_SEEN_SECONDS,
        message="the backend worker stopped from outside never read recycling on the census",
    )
    assert "done" not in _values(), f"the job completed before its worker was seen leaving: {row}"
    assert row.generation == before_backend[running_job], row
    assert row.drain_seconds is not None, f"the leaving backend announced no drain: {row}"
    assert row.drain_seconds == _announced_backend_drain(stack), row
    assert row.drain_seconds > _OUTSIDE_STOP_PROBE_SECONDS, f"the announced drain does not cover the job: {row}"

    name = uniq("outsidestopprofile")
    await _profile_from_stored(stack, {_RECYCLE_VAR: uniq("mkey").upper()}, name)
    applied = await api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True, timeout=200.0)

    # The fan-out finds the stopping worker leaving, not applying: its outcome is ``recycling``, which
    # the change logs loudly as not converged, and its roll waits that worker's next life out.
    outcomes = {r["name"]: r["outcome"] for r in applied["fanout"]["results"]}
    assert outcomes.pop(running_job) == "recycling", applied["fanout"]
    assert set(outcomes.values()) == {"applied"}, applied["fanout"]
    assert applied["recycle_stopped"] is None, applied
    for entry in applied["recycle"]:
        assert entry["status"] in ("recycled", "self-deferred"), f"a row did not converge: {entry}"
    stack.wait_generation_bump(running_job, before_backend[running_job], deadline=150.0)
    wait_for(
        lambda: "done" in _values(), deadline=90.0, message="the in-flight backend job was LOST (no 'done' marker)"
    )
    done_pids = [json.loads(rec)["pid"] for rec in stack.records(key) if json.loads(rec)["value"] == "done"]
    assert done_pids == [started_pid], f"the job did not run to its end once on its own life: {done_pids}"

    # The applier recycles itself after replying.
    stack.wait_generation_bump("serve", before_serve, deadline=150.0)

    async def serves_again() -> bool:
        resp = await stack.api().request_raw("GET", "/health")
        return resp.status_code == 200

    await wait_for_async(serves_again, deadline=60.0, message="the respawned applier never served /health again")
    _assert_no_forbidden_lines(
        stack, offsets, expected=(f"did not fully converge — unconfirmed workers: [('{running_job}', 'recycling', ",)
    )


@pytest.mark.timeout(600)
@pytest.mark.needs(*_SUPERVISED_RECYCLE_NEEDS, "probe-tools", "setting:extension:async_task")
async def test_a_serve_worker_stopped_from_outside_mid_call_reads_leaving_through_its_graceful_stop(
    own_recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A serve worker whose stop is decided outside it while it answers a call reads ``recycling``
    on the census from that stop's first signal, inside its server's graceful stop, announcing its
    whole stop's drain; the call in flight is answered. A roll that reaches it meanwhile finds it
    leaving and waits for its next life: the roll converges, every backend worker restarts once,
    and no worker logs a forbidden line."""
    stack = own_recycle_stack
    [serve] = [worker for worker in stack.census() if worker.kind == "serve"]
    before_backend = _lives(stack, "backend")
    writer = _slot_of(stack, "backend")
    offsets = _log_offsets(stack)
    queue_hold, call_hold = uniq("queuehold"), uniq("callhold")

    try:
        # The held queue worker keeps the roll from reaching the serve worker until the serve
        # worker's stop began.
        await _hold_the_queue_worker(stack, queue_hold)
        task_id = await _apply_a_profile_as_a_backend_job_until_serve_reloads(stack, uniq)

        async with stack.mcp(auth=stack.auth_token) as mcp:
            call = asyncio.create_task(mcp.call_tool("e2e_hold_until_released", {"key": call_hold}))

            async def call_held() -> bool:
                return await asyncio.to_thread(_held_by, stack, call_hold) != []

            await wait_for_async(call_held, deadline=30.0, message="the held call never started")
            assert _held_by(stack, call_hold) == [serve.pid], f"the call ran outside the serve worker {serve}"

            _stop_from_outside(stack, "serve", signal.SIGTERM)
            row = await wait_for_async(
                lambda: _leaving_row(stack, serve.name),
                deadline=_LEAVING_SEEN_SECONDS,
                message="the serve worker stopped from outside never read recycling during its graceful stop",
            )
            assert not call.done(), f"the call ended before the serve worker was seen leaving: {row}"
            assert (row.generation, row.pid) == (serve.generation, serve.pid), row
            assert row.drain_seconds == _announced_serve_drain(stack), row

            stack.infra.redis.release(queue_hold)
            stack.infra.redis.release(call_hold)
            answered = await call
        assert not answered.is_error, answered
    finally:
        stack.infra.redis.release(queue_hold)
        stack.infra.redis.release(call_hold)

    stack.wait_generation_bump("serve", {serve.name: serve.generation}, deadline=200.0)
    applied = _tool_map(await _backend_job_result(stack, task_id))
    assert applied["refused"] == [], applied
    await _assert_the_backend_worker_carries_the_roll(
        stack, applied, writer, {**before_backend, serve.name: serve.generation}, offsets
    )


# A hot-class int settings field read at every build: a non-integer passes the save-time validator
# and fails the build.
_BUILD_POISON_VAR = "ACCESS_CONTROL_CACHE_SIZE"


@pytest.mark.timeout(480)
@pytest.mark.needs(*_SUPERVISED_RECYCLE_NEEDS, "probe-tools", "setting:extension:async_task")
async def test_a_profile_whose_build_fails_applied_as_a_backend_job_persists_nothing(
    recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A profile whose build fails, applied as a backend job: the job fails naming the settings
    error, the stored env, the serving generation and every worker's life are unchanged, and no
    worker logged a forbidden line."""
    api = recycle_stack.api()
    before_env = (await api.get("/api/config/env"))["env"]
    before_epoch = await _settings_epoch(recycle_stack)
    before_lives = {**_lives(recycle_stack, "backend"), **_lives(recycle_stack, "serve")}
    offsets = _log_offsets(recycle_stack)
    name = uniq("jobpoison")
    await api.put(
        f"/api/config/profiles/{name}",
        json={"env": {**before_env, _BUILD_POISON_VAR: "not-an-integer"}},
        retry_on_reloading=True,
    )

    task_id = await _queue_as_backend_job(recycle_stack, "apply_profile", {"name": name})
    result = await _backend_job_result(recycle_stack, task_id)

    text = " ".join(getattr(part, "text", "") for part in result.content)
    assert result.is_error, f"a profile whose build fails was applied from a backend job: {result!r}"
    assert "AccessControlSettings" in text, f"the job's failure does not name the settings error: {text}"
    after_env = (await api.get("/api/config/env"))["env"]
    assert after_env == before_env, f"a failed build persisted the profile: {before_env} -> {after_env}"
    assert _BUILD_POISON_VAR not in after_env, after_env
    assert await _settings_epoch(recycle_stack) == before_epoch
    assert {**_lives(recycle_stack, "backend"), **_lives(recycle_stack, "serve")} == before_lives
    _assert_no_forbidden_lines(recycle_stack, offsets)


@pytest.mark.timeout(480)
@pytest.mark.skipif(
    HarnessSettings().is_real("llm"),
    reason="scripted llm_stub is the 'llm' mock leg; the real leg runs under TAI_E2E_REAL",
)
@pytest.mark.needs(*_SUPERVISED_RECYCLE_NEEDS, "helper:llm", "setting:agent:tools_agent", "kind:tool_runs")
async def test_an_env_write_by_an_agent_rolls_the_fleet_and_the_writer_recycles_itself_after_the_run(
    own_recycle_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """An agent run whose tool call is ``write_env`` of a recycle-class key rolls the backend, the
    tool result the model reads carries the serve writer ``self-deferred`` and nobody awaiting a
    respawn, the run ends ``succeeded`` with the model's final answer, and the serve worker then
    exits and its supervisor respawns it."""
    before_backend = _lives(own_recycle_stack, "backend")
    before_serve = _lives(own_recycle_stack, "serve")
    offsets = _log_offsets(own_recycle_stack)
    final = f"written {uniq('run')}"
    llm_stub.reset()
    llm_stub.script(
        [
            {"tool_call": {"name": "write_env", "arguments": {"env": {_RECYCLE_VAR: uniq("mkey").upper()}}}},
            {"content": final},
        ]
    )

    api = own_recycle_stack.api()
    submitted = await api.post(
        "/api/tool-runs",
        json={
            "tool_name": "tools_agent",
            "arguments": {"user_message": {"content": "write the setting"}, "tool_names": ["write_env"]},
        },
        expect=202,
        retry_on_reloading=True,
    )
    run_id = submitted["run_id"]

    # The serve worker exits once the run it drains has ended, and comes back at its next life.
    own_recycle_stack.wait_generation_bump("serve", before_serve, deadline=200.0)

    async def _terminal() -> dict[str, Any] | None:
        view = await own_recycle_stack.api().get(f"/api/tool-runs/{run_id}", retry_on_reloading=True)
        return view if view["status"] not in ("pending", "running") else None

    view = await wait_for_async(_terminal, deadline=60.0, message=f"agent run {run_id} never ended")
    assert view["status"] == "succeeded", f"the agent run did not complete: {view}"
    assert final in json.dumps(view["result"]), f"the run did not return the model's final answer: {view}"

    requests = llm_stub.requests
    assert len(requests) == 2, f"expected 2 LLM round-trips, saw {len(requests)}"
    tool_messages = [m for m in requests[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1, f"the second completion carried no single tool result: {requests[1]['messages']}"
    written = json.loads(tool_messages[0]["content"])
    assert written["awaiting_respawn"] == [], written
    await _assert_roll_converged_and_writer_respawns(
        own_recycle_stack, written["recycle"], before_backend, before_serve
    )
    _assert_the_writer_left_once_for_its_own_change(own_recycle_stack, offsets)
    _assert_no_forbidden_lines(own_recycle_stack, offsets)
