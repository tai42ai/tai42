"""A fleet reload restarts the workers that report a recycle-class key off its bound value.

A recycle-class value changed in the stored env outside the pipeline (a hand edit of the
``.env``) and then a fleet reload: the soft restart carries every hot key in place, so each
worker answers ``applied`` and states, in its own payload, the recycle-class keys whose value
differs from the one it bound at boot. The worker that ran the reload command decides from
those reports: every worker naming such a key is restarted — each backend sibling recycled
through its supervisor, the writer's own restart its graceful exit once the reply is answered —
and the reply names that roll under ``recycle`` with the keys that caused each restart. The
respawned workers bound the edited value at boot: the next reload names nothing and restarts
nobody.

The harness ``supervised`` mode runs the respawn-on-exit supervisor, so a recycled or
self-exited worker actually comes back.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import child_env, wait_for_async
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.manifests import build_recycle_stack
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs(
    "kind:backend",
    "mutable",
    "store:redis",
    "process",
    "files",
    "topology:supervised",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
    "setting:TAI_BUS_APPLY_TIMEOUT=330",
)

# A recycle-class field on the loaded backend (env ``BACKEND_MANIFEST_KEY``) the deployment env
# does not set.
_RECYCLE_VAR = "BACKEND_MANIFEST_KEY"


def _lives(stack: TaiStack) -> dict[str, tuple[str, int]]:
    """The live census ``{name: (kind, generation)}``."""
    return {worker.name: (worker.kind, worker.generation) for worker in stack.census()}


def _awaiting(report: dict[str, Any]) -> dict[str, list[str]]:
    """Each worker's ``awaiting_respawn`` list from a fleet reload report, every worker ``applied``."""
    assert report["ok"] is True, report
    rows: dict[str, list[str]] = {}
    for row in report["results"]:
        assert row["outcome"] == "applied", row
        rows[row["name"]] = row["payload"]["awaiting_respawn"]
    return rows


async def _reload(stack: TaiStack) -> dict[str, Any]:
    return await stack.api().post(
        "/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True, timeout=200.0
    )


@pytest.mark.timeout(420)
async def test_a_reload_after_a_hand_edit_restarts_every_worker_reporting_the_recycle_class_key(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(build_recycle_stack, resource_kwargs={"llm_base_url": llm_stub.base_url})
    before = _lives(stack)

    # The out-of-band edit: the stored .env gains a new value for a recycle-class key.
    stack.config.stored_env[_RECYCLE_VAR] = uniq("handedit").upper()
    child_env.render_env_file(stack)

    report = await _reload(stack)

    awaiting = _awaiting(report)
    assert set(awaiting) == set(before), f"a census worker was absent from the reload: {awaiting} vs {before}"
    assert all(keys == [_RECYCLE_VAR] for keys in awaiting.values()), awaiting
    roll = report["recycle"]
    assert roll is not None, report
    assert roll["recycle_stopped"] is None, f"the recycle roll stopped: {roll}"
    assert all(row["keys"] == [_RECYCLE_VAR] for row in roll["recycle"]), roll
    backend_rows = [row for row in roll["recycle"] if row["kind"] == "backend"]
    assert backend_rows, roll
    for row in backend_rows:
        assert row["status"] == "recycled", row
        assert row["generation_before"] == before[row["name"]][1], (row, before)
    # A recycled worker's new life joins and restarts every sibling still reporting the key, so
    # the roll may reach a backend the join already restarted: asked once ready, that new life
    # confirms the edited value and is left running, named under ``skipped`` and ``fresh``.
    fresh = {life["name"]: life["generation"] for life in roll["fresh"]}
    for name in roll["skipped"]:
        assert before[name][0] == "backend", (name, roll)
        assert fresh.get(name, 0) > before[name][1], (name, roll, before)
    assert {row["name"] for row in backend_rows} | set(roll["skipped"]) == {
        name for name, (kind, _generation) in before.items() if kind == "backend"
    }, (roll, before)
    writer = [row for row in roll["recycle"] if row["status"] == "self-deferred"]
    assert [row["kind"] for row in writer] == ["serve"], roll
    assert report["awaiting_respawn"] == [], report

    stack.wait_generation_bump("serve", {name: gen for name, (kind, gen) in before.items() if kind == "serve"})

    async def everyone_respawned() -> bool:
        now = _lives(stack)
        return all(name in now and now[name][1] > generation for name, (_kind, generation) in before.items())

    await wait_for_async(everyone_respawned, deadline=150.0, message="a worker never respawned after the roll")

    # Each worker restarted exactly once, by the roll or by a restarted worker's join.
    respawned = _lives(stack)
    assert respawned == {name: (kind, generation + 1) for name, (kind, generation) in before.items()}, (
        respawned,
        before,
    )

    # Every worker bound the edited value at boot: the next reload names nothing and restarts nobody.
    report = await _reload(stack)
    assert all(keys == [] for keys in _awaiting(report).values()), report
    assert report["recycle"] is None, report
    assert report["awaiting_respawn"] == [], report
    assert _lives(stack) == respawned
