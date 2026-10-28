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

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.binaries import tai_bin
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for

pytestmark = pytest.mark.needs("kind:backend")

# A recycle-class field on the loaded backend (env ``BACKEND_MANIFEST_KEY``) the deployment env
# does not set — so a diff on it rolls a recycle rather than being refused.
_RECYCLE_VAR = "BACKEND_MANIFEST_KEY"


def _lives(stack: TaiStack, kind: str) -> dict[str, int]:
    """The live ``{name: generation}`` of a kind on the bus census — the snapshot the
    recycle convergence proof reads (old lives gone + fresh capacity, both keyed on the
    stable slot name and its monotonic generation, never a fresh identity string)."""
    return {worker.name: worker.generation for worker in stack.census() if worker.kind == kind}


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
    recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = recycle_stack.api()
    before_backend = _lives(recycle_stack, "backend")
    before_serve = _lives(recycle_stack, "serve")
    assert before_backend, "no backend life before the recycle"
    assert before_serve, "no serve life before the recycle"

    name = uniq("recycle")
    await _profile_from_stored(recycle_stack, {_RECYCLE_VAR: uniq("mkey").upper()}, name)

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
    # Every backend target converged: a row the roll stopped at (timed-out / failed) names
    # why in its detail; a recycled row carries none.
    for e in backend_rows:
        assert e["status"] == "recycled", f"a backend row did not converge: {e} in {applied}"
        assert e["detail"] is None, f"a recycled backend row carries a detail: {e}"
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
        live = _lives(recycle_stack, "backend")
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

    # Single-gap deterministic rolling recycle (one backend under the respawn supervisor):
    # the fresh backend set IS the same names at generation+1 — a stronger mechanical check
    # that holds only because this scenario reuses each slot's name for its next life.
    fresh_by_name = {f["name"]: f["generation"] for f in fresh_backend}
    for e in backend_rows:
        assert fresh_by_name.get(e["name"]) == e["generation_before"] + 1, (
            f"the recycled backend {e['name']!r} did not reappear at generation+1: fresh={fresh_backend} row={e}"
        )

    # The applier serve worker self-exited (deferred, after the response) and respawned under
    # its stable name at a bumped generation; it serves /health again.
    recycle_stack.wait_generation_bump("serve", before_serve, deadline=150.0)

    async def serves_again() -> bool:
        resp = await api.request_raw("GET", "/health")
        return resp.status_code == 200

    await wait_for_async(serves_again, deadline=60.0, message="the respawned applier never served /health again")


@pytest.mark.timeout(300)
@pytest.mark.needs(
    "probe-tools",
    "mutable",
    "store:redis",
    "process",
    "topology:supervised",
    "setting:extension:sync_task",
)
async def test_recycle_drains_in_flight_backend_job(recycle_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """The in-flight backend job is DRAINED, not lost: a job started before the recycle and
    still executing when the recycle self-exit fires completes on the OLD process during its
    graceful drain (the arq job-completion-wait fix)."""
    api = recycle_stack.api()
    key = uniq("drain")
    serve_pids = {worker.pid for worker in recycle_stack.census() if worker.kind == "serve"}

    # Start a background run of the sync_task-branched drain probe: the tool-runs supervisor
    # (a serve worker) dispatches it INTO the arq backend, which RPUSHes a ``started`` marker
    # (its own backend pid), sleeps, then RPUSHes ``done`` on completion.
    await api.post(
        "/api/tool-runs",
        json={"tool_name": "e2e_drain_probe_sync_task", "arguments": {"key": key, "seconds": 12}},
        expect=202,
    )

    def _values() -> list[str]:
        return [json.loads(rec)["value"] for rec in recycle_stack.records(key)]

    wait_for(lambda: "started" in _values(), deadline=30.0, message="the backend job never reported it was in-flight")
    started_pid = json.loads(recycle_stack.records(key)[0])["pid"]
    assert started_pid not in serve_pids, f"the job ran on a serve worker ({started_pid}), not a backend"
    assert "done" not in _values(), "the backend job completed before the recycle could fire mid-flight"

    before_backend = _lives(recycle_stack, "backend")
    before_serve = _lives(recycle_stack, "serve")
    name = uniq("drainrecycle")
    await _profile_from_stored(recycle_stack, {_RECYCLE_VAR: uniq("mkey").upper()}, name)
    await api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True, timeout=200.0)

    # The recycle rolled (the backend life bumped to its next generation under its stable name).
    recycle_stack.wait_generation_bump("backend", before_backend, deadline=150.0)

    # DRAINED, NOT LOST: the in-flight arq job completed during the backend's graceful recycle
    # shutdown (the arq job-completion-wait behaviour), so its ``done`` marker lands — and it
    # was written by the SAME (pre-recycle) backend pid that started it, proving the ORIGINAL
    # job drained rather than a respawned worker re-running it.
    wait_for(
        lambda: "done" in _values(), deadline=90.0, message="the in-flight backend job was LOST (no 'done' marker)"
    )
    done_pids = {json.loads(rec)["pid"] for rec in recycle_stack.records(key) if json.loads(rec)["value"] == "done"}
    assert started_pid in done_pids, f"'done' came from a different pid than started ({started_pid} vs {done_pids})"

    # The applier recycles itself after replying: the module's later legs start on its respawned life.
    recycle_stack.wait_generation_bump("serve", before_serve, deadline=150.0)

    async def serves_again() -> bool:
        resp = await recycle_stack.api().request_raw("GET", "/health")
        return resp.status_code == 200

    await wait_for_async(serves_again, deadline=60.0, message="the respawned applier never served /health again")


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
    "cli",
    "topology:supervised",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
    "setting:TAI_BUS_APPLY_TIMEOUT=330",
)
async def test_an_env_write_of_a_recycle_class_key_rolls_the_fleet_and_self_defers_the_writer(
    recycle_stack: TaiStack, uniq: Callable[[str], str], tmp_path: Path
) -> None:
    """``POST /api/config/env`` of a recycle-class key on a supervised stack rolls the same
    recycle a profile apply rolls and answers its rows under ``recycle``; ``tai config env set``
    over the CLI prints the rows of the next one."""
    api = recycle_stack.api()
    before_backend = _lives(recycle_stack, "backend")
    before_serve = _lives(recycle_stack, "serve")

    written = await api.post(
        "/api/config/env", json={"env": {_RECYCLE_VAR: uniq("mkey").upper()}}, retry_on_reloading=True, timeout=200.0
    )
    assert written["fanout"]["mode"] in ("fleet", "local-only"), written
    # The operation's verdict: a converged roll with the writer recycling itself leaves nobody.
    assert written["awaiting_respawn"] == [], written
    await _assert_roll_converged_and_writer_respawns(recycle_stack, written["recycle"], before_backend, before_serve)

    before_backend = _lives(recycle_stack, "backend")
    before_serve = _lives(recycle_stack, "serve")
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": str(tmp_path),
        "TAI_SERVER_URL": recycle_stack.origin(recycle_stack.port_a),
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
    await _assert_roll_converged_and_writer_respawns(recycle_stack, printed["recycle"], before_backend, before_serve)


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
    recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """The projected ``write_env`` tool (MCP ``tools/call``) of a recycle-class key rolls the same
    recycle the HTTP door rolls, answers the writer's ``self-deferred`` row, and the writer then
    exits gracefully once the call returned: its supervisor respawns it at a bumped generation."""
    before_backend = _lives(recycle_stack, "backend")
    before_serve = _lives(recycle_stack, "serve")

    async with recycle_stack.mcp(auth=recycle_stack.auth_token) as mcp:
        # The write's reload retires this session and the writer then exits: the tool listing
        # is read first so the client asks this session nothing after the call answered.
        await mcp.list_tools()
        result = await mcp.call_tool(
            "write_env", {"env": {_RECYCLE_VAR: uniq("mkey").upper()}}, retry_on_reloading=True, ready_deadline=30.0
        )
    written = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(written, dict), f"write_env returned no result map: {result!r}"
    assert written["awaiting_respawn"] == [], written
    await _assert_roll_converged_and_writer_respawns(recycle_stack, written["recycle"], before_backend, before_serve)


@pytest.mark.timeout(420)
@pytest.mark.needs(*_SUPERVISED_RECYCLE_NEEDS)
async def test_a_backup_env_restore_of_a_recycle_class_key_rolls_the_fleet_and_self_defers_the_writer(
    recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """``POST /api/backup/import`` of an ``env`` section carrying a recycle-class key is an env
    write: the section's report carries the roll under ``recycle`` and an empty verdict, and the
    writer recycles itself after replying."""
    api = recycle_stack.api()
    before_backend = _lives(recycle_stack, "backend")
    before_serve = _lives(recycle_stack, "serve")

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
    await _assert_roll_converged_and_writer_respawns(recycle_stack, section["recycle"], before_backend, before_serve)


# The lines a converged env write never logs, on any worker: the operation's verdict naming a
# worker nothing respawns, a fleet reload a worker did not confirm, and a writer exit that failed.
_FORBIDDEN_LINES = (
    "worker(s) awaiting a respawn",
    "did not fully converge",
    "post-reply task failed",
)


def _log_offsets(stack: TaiStack) -> dict[str, int]:
    """The current length of the serve and backend logs, so a leg reads only what it caused."""
    return {name: len(_read_log(stack, name)) for name in ("serve", "backend")}


def _read_log(stack: TaiStack, name: str) -> str:
    return stack.process(name).log_path.read_text(encoding="utf-8", errors="replace")


def _assert_no_forbidden_lines(stack: TaiStack, offsets: dict[str, int]) -> None:
    for name, offset in offsets.items():
        text = _read_log(stack, name)
        for line in text[offset:].splitlines():
            assert not any(marker in line for marker in _FORBIDDEN_LINES), f"{name} logged: {line}"


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
    recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """``write_env`` queued as a backend job (``run_tool_async_task``, the queue a schedule fires
    through) is written by the backend worker that ran the job: the roll recycles the serve
    fleet, the job's result reports that backend worker ``self-deferred`` and nobody awaiting a
    respawn, and the backend worker finishes the job, exits, and its supervisor respawns it."""
    before_backend = _lives(recycle_stack, "backend")
    before_serve = _lives(recycle_stack, "serve")
    offsets = _log_offsets(recycle_stack)

    task_id = await _queue_as_backend_job(recycle_stack, "write_env", {"env": {_RECYCLE_VAR: uniq("mkey").upper()}})

    # The roll recycles the serve worker; the respawned serve reads the job's recorded result.
    recycle_stack.wait_generation_bump("serve", before_serve, deadline=150.0)
    written = _tool_map(await _backend_job_result(recycle_stack, task_id))

    assert written["awaiting_respawn"] == [], written
    _assert_every_worker_applied(written["fanout"])
    roll = written["recycle"]
    await _assert_the_backend_worker_wrote_and_recycles(
        recycle_stack, roll["recycle"], roll["recycle_stopped"], before_backend, before_serve, offsets
    )


def _assert_every_worker_applied(fanout: dict[str, Any]) -> None:
    """The reload fan-out a backend job published reached the whole fleet, every worker ``applied`` —
    the worker running the job included, whose pool turnover does not wait for that job."""
    assert fanout["mode"] == "fleet", fanout
    assert fanout["results"], fanout
    for row in fanout["results"]:
        assert row["outcome"] == "applied", f"{row} in {fanout}"


async def _assert_the_backend_worker_wrote_and_recycles(
    stack: TaiStack,
    rows: list[dict[str, Any]],
    stopped: Any,
    before_backend: dict[str, int],
    before_serve: dict[str, int],
    offsets: dict[str, int],
) -> None:
    """A backend job's roll: the serve rows ``recycled`` from their pre-write lives, exactly one
    ``self-deferred`` row naming the backend worker that ran the job; then that worker exits after
    the job and comes back at its next life, the serve answers ``/health``, and no worker logged a
    forbidden line."""
    assert stopped is None, f"the recycle roll stopped: {stopped} in {rows}"
    serve_rows = [e for e in rows if e["kind"] == "serve"]
    assert serve_rows, f"serve not recycled: {rows}"
    for e in serve_rows:
        assert e["status"] == "recycled", f"a serve row did not converge: {e} in {rows}"
        assert e["generation_before"] == before_serve[e["name"]], f"not the pre-write life: {e} vs {before_serve}"
    writer = [e for e in rows if e["status"] == "self-deferred"]
    assert len(writer) == 1, f"expected exactly one self-deferred writer row: {rows}"
    assert writer[0]["kind"] == "backend", writer
    assert writer[0]["name"] in before_backend, (
        f"the writer named an unknown backend slot: {writer} vs {before_backend}"
    )
    assert writer[0]["generation_before"] == before_backend[writer[0]["name"]], writer

    stack.wait_generation_bump(writer[0]["name"], before_backend[writer[0]["name"]], deadline=150.0)

    async def serves_again() -> bool:
        resp = await stack.api().request_raw("GET", "/health")
        return resp.status_code == 200

    await wait_for_async(serves_again, deadline=60.0, message="the respawned serve never served /health again")
    _assert_no_forbidden_lines(stack, offsets)


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
    recycle_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """``apply_profile`` queued as a backend job: the job proves the profile builds, persists it,
    the roll recycles the serve, the job's result names the backend worker that ran it
    ``self-deferred`` with nobody awaiting a respawn, and that worker exits after the job."""
    before_backend = _lives(recycle_stack, "backend")
    before_serve = _lives(recycle_stack, "serve")
    offsets = _log_offsets(recycle_stack)
    name = uniq("jobprofile")
    await _profile_from_stored(recycle_stack, {_RECYCLE_VAR: uniq("mkey").upper()}, name)

    task_id = await _queue_as_backend_job(recycle_stack, "apply_profile", {"name": name})
    recycle_stack.wait_generation_bump("serve", before_serve, deadline=150.0)
    applied = _tool_map(await _backend_job_result(recycle_stack, task_id))

    assert applied["refused"] == [], applied
    assert applied["awaiting_respawn"] == [], applied
    _assert_every_worker_applied(applied["fanout"])
    await _assert_the_backend_worker_wrote_and_recycles(
        recycle_stack, applied["recycle"], applied["recycle_stopped"], before_backend, before_serve, offsets
    )


# A hot-class int settings field read at every build: a non-integer passes the save-time validator
# and fails the build.
_BUILD_POISON_VAR = "ACCESS_CONTROL_CACHE_SIZE"


async def _settings_epoch(stack: TaiStack) -> int:
    async with stack.mcp(auth=stack.auth_token) as mcp:
        snapshot = _tool_map(await mcp.call_tool("e2e_settings_snapshot", {}, retry_on_reloading=True))
    return int(snapshot["settings_epoch"])


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
    reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
)
@pytest.mark.needs(*_SUPERVISED_RECYCLE_NEEDS, "helper:llm", "setting:agent:tools_agent", "kind:tool_runs")
async def test_an_env_write_by_an_agent_rolls_the_fleet_and_the_writer_recycles_itself_after_the_run(
    recycle_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """An agent run whose tool call is ``write_env`` of a recycle-class key rolls the backend, the
    tool result the model reads carries the serve writer ``self-deferred`` and nobody awaiting a
    respawn, the run ends ``succeeded`` with the model's final answer, and the serve worker then
    exits and its supervisor respawns it."""
    before_backend = _lives(recycle_stack, "backend")
    before_serve = _lives(recycle_stack, "serve")
    offsets = _log_offsets(recycle_stack)
    final = f"written {uniq('run')}"
    llm_stub.reset()
    llm_stub.script(
        [
            {"tool_call": {"name": "write_env", "arguments": {"env": {_RECYCLE_VAR: uniq("mkey").upper()}}}},
            {"content": final},
        ]
    )

    api = recycle_stack.api()
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
    recycle_stack.wait_generation_bump("serve", before_serve, deadline=200.0)

    async def _terminal() -> dict[str, Any] | None:
        view = await recycle_stack.api().get(f"/api/tool-runs/{run_id}", retry_on_reloading=True)
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
    await _assert_roll_converged_and_writer_respawns(recycle_stack, written["recycle"], before_backend, before_serve)
    _assert_no_forbidden_lines(recycle_stack, offsets)
