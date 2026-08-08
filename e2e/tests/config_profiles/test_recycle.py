"""F5 — the settings-profile RECYCLE leg.

A profile whose diff carries a RECYCLE-class key on a SUPERVISED shape rolls a fleet
recycle (``config/service.py::apply_replace_env`` STEP 5 → ``orchestrate_recycle``): each
targeted sibling replies ``applied`` then gracefully self-exits, an external supervisor
respawns it, and its replacement rejoins the bus census under the SAME stable slot name at
a bumped generation. Recycle is confirmed on REALITY (D7): each recycled row carries its
pre-recycle ``generation_before``, the recycled targets' old lives leave presence, and the
response ``fresh`` list counts NEW READY capacity per kind. The applying serve worker's OWN
recycle is a deferred post-response self-exit it reports as ``self-deferred``. On a BARE
(unsupervised) shape the same apply is refused upfront at the API, naming the recycle-class
key.

Validated locally against the isolated infra: the harness ``supervised`` mode stamps
``TAI_SUPERVISED=harness`` and runs the respawn-on-exit supervisor, so a recycled/self-exited
worker actually comes back. ``BACKEND_MANIFEST_KEY`` is a recycle-class field (``reload_class
= "recycle"``) not in any refused tier on the ``harness`` shape.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for

# A recycle-class field on the loaded backend (env ``BACKEND_MANIFEST_KEY``), not in the
# harness shape's refused set — so a diff on it rolls a recycle rather than being refused.
_RECYCLE_VAR = "BACKEND_MANIFEST_KEY"


def _lives(stack: TaiStack, kind: str) -> dict[str, int]:
    """The live ``{name: generation}`` of a kind on the bus census — the snapshot the D7
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

    # The recycle report: each recycled backend sibling row carries its pre-recycle
    # generation (never a post-recycle life), and the applying serve worker's own recycle is
    # the deferred self-exit carrying its own name + current generation.
    recycle = applied["recycle"]
    fresh = applied["fresh"]
    backend_rows = [e for e in recycle if e["kind"] == "backend" and e["status"] == "recycled"]
    assert backend_rows, f"backend not recycled: {applied}"
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

    # D7 reality — OLD lives gone: each recycled backend target's pre-recycle life has left
    # presence (its name absent, or a higher generation on that name). The orchestrator
    # confirmed this before responding; assert it directly against the census.
    recycled_backends = {e["name"]: e["generation_before"] for e in backend_rows}

    async def old_backend_lives_gone() -> bool:
        live = _lives(recycle_stack, "backend")
        return all(name not in live or live[name] > gen for name, gen in recycled_backends.items())

    await wait_for_async(
        old_backend_lives_gone, deadline=90.0, message="a recycled backend's old life never left the census"
    )

    # D7 reality — counted READY capacity: the response `fresh` list carries a NEW READY
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
