"""F5 — the settings-profile RECYCLE leg.

A profile whose diff carries a RECYCLE-class key on a SUPERVISED shape rolls a fleet
recycle (``config/service.py::apply_replace_env`` STEP 5 → ``orchestrate_recycle``): each
targeted sibling replies ``applied`` then gracefully self-exits, an external supervisor
respawns it, and its replacement rejoins the bus census under a fresh origin. The applying
serve worker's OWN recycle is a deferred post-response self-exit it reports as
``self-deferred``. On a BARE (unsupervised) shape the same apply is refused upfront at the
API, naming the recycle-class key.

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


def _origins(stack: TaiStack, kind: str) -> set[str]:
    return {origin.origin for origin in stack.census() if origin.kind == kind}


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
    before_backend = _origins(recycle_stack, "backend")
    before_serve = _origins(recycle_stack, "serve")
    assert before_backend, "no backend origin before the recycle"
    assert before_serve, "no serve origin before the recycle"

    name = uniq("recycle")
    await _profile_from_stored(recycle_stack, {_RECYCLE_VAR: uniq("mkey").upper()}, name)

    # The apply blocks through the orchestrated backend recycle (self-exit → respawn →
    # rejoin) before it returns, so give the client room.
    applied = await api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True, timeout=200.0)
    assert applied["refused"] == [], f"a recycle apply on a supervised shape refused a key: {applied}"

    # The recycle report: the backend sibling was recycled, and the applying serve worker's
    # own recycle is the deferred self-exit.
    recycle = applied["recycle"]
    assert any(e["kind"] == "backend" and e["status"] == "recycled" for e in recycle), (
        f"backend not recycled: {applied}"
    )
    assert any(e["status"] == "self-deferred" for e in recycle), f"applier self-exit not deferred: {applied}"

    # OLD backend origin gone, a NEW backend origin joined (the respawn rejoined the census).
    new_backend = recycle_stack.wait_new_origin("backend", before_backend, count=1, deadline=150.0)
    assert new_backend, "no new backend origin joined after the recycle"

    async def old_backend_gone() -> bool:
        return not (before_backend & _origins(recycle_stack, "backend"))

    await wait_for_async(old_backend_gone, deadline=90.0, message="the recycled backend origin never left the census")

    # The applier serve worker self-exited (deferred, after the response) and respawned.
    recycle_stack.wait_new_origin("serve", before_serve, count=1, deadline=150.0)

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
    serve_pids = {origin.pid for origin in recycle_stack.census() if origin.kind == "serve"}

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

    before_backend = _origins(recycle_stack, "backend")
    name = uniq("drainrecycle")
    await _profile_from_stored(recycle_stack, {_RECYCLE_VAR: uniq("mkey").upper()}, name)
    await api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True, timeout=200.0)

    # The recycle rolled (a new backend origin joined).
    recycle_stack.wait_new_origin("backend", before_backend, count=1, deadline=150.0)

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
