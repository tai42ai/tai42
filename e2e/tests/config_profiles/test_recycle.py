"""The settings-profile RECYCLE leg.

A profile whose diff carries a RECYCLE-class key on a SUPERVISED shape rolls a fleet
recycle (``config/service.py::apply_replace_env`` STEP 5 → ``orchestrate_recycle``): each
targeted sibling replies ``applied`` then gracefully self-exits, an external supervisor
respawns it, and its replacement rejoins the bus census under the SAME stable slot name at
a bumped generation. Recycle is confirmed on REALITY: each recycled row carries its
pre-recycle ``generation_before``, the recycled targets' old lives leave presence, and the
response ``fresh`` list counts NEW READY capacity per kind. The applying serve worker's OWN
recycle is a deferred post-response self-exit it reports as ``self-deferred``. On a BARE
(unsupervised) shape the same apply is refused upfront at the API, naming the recycle-class
key. The env-write door follows the same policy: on a supervised shape an env write of a
recycle-class key rolls the same recycle and answers its rows under ``recycle`` (the writer
self-defers), on a bare shape it is refused, and the bus URL is refused on every shape, each
refusal naming the key with nothing written.

Validated locally against the isolated infra: the harness ``supervised`` mode stamps
``TAI_SUPERVISED=harness`` and runs the respawn-on-exit supervisor, so a recycled/self-exited
worker actually comes back. ``BACKEND_MANIFEST_KEY`` is a recycle-class field (``reload_class
= "recycle"``) not in any refused tier on the ``harness`` shape.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.binaries import tai_bin
from tai42_e2e.manifests import build_recycle_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.topology import StackConfig, StackResources
from tai42_e2e.waiting import wait_for

if TYPE_CHECKING:
    from tai42_e2e.variants import Variants

pytestmark = pytest.mark.needs("kind:backend")

# A recycle-class field on the loaded backend (env ``BACKEND_MANIFEST_KEY``), not in the
# harness shape's refused set — so a diff on it rolls a recycle rather than being refused.
_RECYCLE_VAR = "BACKEND_MANIFEST_KEY"


def _lives(stack: TaiStack, kind: str) -> dict[str, int]:
    """The live ``{name: generation}`` of a kind on the bus census — the snapshot the
    recycle convergence proof reads (old lives gone + fresh capacity, both keyed on the
    stable slot name and its monotonic generation, never a fresh identity string)."""
    return {worker.name: worker.generation for worker in stack.census() if worker.kind == kind}


async def _profile_from_stored(stack: TaiStack, changes: dict[str, str], name: str) -> None:
    """Save a profile that is the CURRENT stored env plus ``changes`` — so every
    deployment-pinned / recycle key stays at its current value (zero diff, no refusal) and
    the only diff is ``changes``."""
    stored = (await stack.api().get("/api/config/env"))["env"]
    await stack.api().put(f"/api/config/profiles/{name}", json={"env": {**stored, **changes}}, retry_on_reloading=True)


@pytest.mark.timeout(300)
@pytest.mark.needs(
    "mutable",
    "store:redis",
    "process",
    "topology:supervised",
    "setting:TAI_SUPERVISED=harness",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
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
    assert applied["refused"] == [], f"a recycle apply on a supervised shape refused a key: {applied}"
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
    "setting:TAI_SUPERVISED=harness",
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


# The synthetic key a compose-marked stack declares pinned.
_PINNED_SAMPLE = "E2E_PINNED_SAMPLE"


def _build_compose_marked_recycle_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The recycle stack under the ``compose`` marker with a declared synthetic pinned set."""
    return replace(
        build_recycle_stack(res, variants),
        name="recycle-compose",
        supervision_marker="compose",
        supervised_pinned_keys=[_PINNED_SAMPLE],
    )


@pytest.mark.timeout(300)
@pytest.mark.needs("mutable", "store:redis", "process", "topology:supervised")
async def test_declared_pinned_key_refuses_a_profile_changing_it(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """On a ``compose``-marked deployment the keys it declares in ``TAI_SUPERVISED_PINNED_KEYS``
    are refused upfront at the API, naming the key: a container respawn would re-inject them."""
    stack = fresh_stack(_build_compose_marked_recycle_stack)
    api = stack.api()
    name = uniq("pinned")
    await _profile_from_stored(stack, {_PINNED_SAMPLE: uniq("value")}, name)
    resp = await api.request_raw("POST", f"/api/config/profiles/{name}/apply")
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert "'compose' deployment" in error, error
    assert _PINNED_SAMPLE in error, error


@pytest.mark.needs("setting:TAI_SUPERVISED=unset")
async def test_bare_shape_refuses_recycle_class_apply(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """On a BARE (unsupervised) shape a recycle-class diff is refused UPFRONT at the API,
    naming the key — no supervisor exists to respawn a recycled worker."""
    api = replicas_stack.api(port=replicas_stack.port_a)
    name = uniq("barerecycle")
    stored = (await api.get("/api/config/env"))["env"]
    await api.put(
        f"/api/config/profiles/{name}",
        json={"env": {**stored, _RECYCLE_VAR: uniq("mkey").upper()}},
        retry_on_reloading=True,
    )
    resp = await api.request_raw("POST", f"/api/config/profiles/{name}/apply")
    assert resp.status_code == 400, resp.text
    assert "bare" in resp.json()["error"].lower(), resp.json()


@pytest.mark.needs("mutable", "setting:TAI_SUPERVISED=unset")
async def test_bare_shape_refuses_a_recycle_class_env_write(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """On a BARE shape the env-write door refuses a recycle-class key UPFRONT, naming it, and
    stores nothing — never an ``applied`` reply while the running workers keep the boot value."""
    api = replicas_stack.api(port=replicas_stack.port_a)
    before = (await api.get("/api/config/env"))["env"]
    resp = await api.request_raw("POST", "/api/config/env", json={"env": {_RECYCLE_VAR: uniq("mkey").upper()}})
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert "bare" in error.lower(), error
    assert _RECYCLE_VAR in error, error
    assert (await api.get("/api/config/env"))["env"] == before, "a refused env write changed the stored env"


@pytest.mark.timeout(300)
@pytest.mark.needs("mutable", "store:redis", "process", "topology:supervised", "setting:TAI_SUPERVISED=harness")
async def test_supervised_shape_refuses_a_bus_url_env_write(recycle_stack: TaiStack) -> None:
    """The bus URL reaches the worker bus itself, so no recycle can carry it: the env-write door
    refuses it on a supervised shape too, naming the key, and stores nothing."""
    api = recycle_stack.api()
    before = (await api.get("/api/config/env"))["env"]
    resp = await api.request_raw(
        "POST", "/api/config/env", json={"env": {"TAI_BUS_REDIS_URL": "redis://127.0.0.1:1/0"}}
    )
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert "TAI_BUS_REDIS_URL" in error, error
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
    "setting:TAI_SUPERVISED=harness",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
)
async def test_an_env_write_of_a_recycle_class_key_rolls_the_fleet_and_self_defers_the_writer(
    recycle_stack: TaiStack, uniq: Callable[[str], str], tmp_path: Path
) -> None:
    """``POST /api/config/env`` of a recycle-class key on a supervised shape rolls the same
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
    "setting:TAI_SUPERVISED=harness",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
)


@pytest.mark.timeout(420)
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
