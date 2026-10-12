"""A reload names the recycle-class keys it cannot carry.

A recycle-class value changed in the stored env outside the pipeline (a hand edit of the
``.env``) and then a fleet reload: the soft restart carries every hot key in place and never
respawns a process, so each worker answers ``applied`` and states, in its own payload, the
recycle-class keys whose value differs from the one it bound at boot. The worker that ran the
reload command judges the report: nothing in a soft restart respawns anyone, so it logs one
ERROR line naming every such worker and its keys. A worker respawned since lists nothing on
its next reload; nothing rolls a recycle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import child_env
from tai42_e2e.manifests import build_core_stack
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:backend", "mutable", "store:redis", "process")

# A recycle-class field on the loaded backend (env ``BACKEND_MANIFEST_KEY``).
_RECYCLE_VAR = "BACKEND_MANIFEST_KEY"
_ERROR_LINE = "worker(s) awaiting a respawn — recycle-class key(s) changed in the stored env since they were bound"


def _kinds(stack: TaiStack) -> dict[str, tuple[str, int]]:
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


def _log(stack: TaiStack, process: str) -> str:
    return stack.process(process).log_path.read_text(encoding="utf-8", errors="replace")


@pytest.mark.timeout(300)
async def test_a_reload_after_a_hand_edit_names_the_recycle_class_key_until_the_worker_respawns(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    stack = fresh_stack(build_core_stack)
    api = stack.api()
    before = _kinds(stack)

    # The out-of-band edit: the stored .env gains a new value for a recycle-class key.
    stack.config.env[_RECYCLE_VAR] = uniq("handedit").upper()
    child_env.render_env_file(stack)

    report = await api.post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)

    awaiting = _awaiting(report)
    assert set(awaiting) == set(before), f"a census worker was absent from the reload: {awaiting} vs {before}"
    assert all(keys == [_RECYCLE_VAR] for keys in awaiting.values()), awaiting
    # The soft restart respawned nothing: every generation is unchanged.
    assert _kinds(stack) == before
    # The publisher (a serve worker) judges the report in one line naming every worker; the
    # backend only stated its fact.
    serve_lines = [line for line in _log(stack, "serve").splitlines() if _ERROR_LINE in line]
    assert len(serve_lines) == 1, serve_lines
    for name in before:
        assert f"{name} ({_RECYCLE_VAR})" in serve_lines[0], serve_lines[0]
    assert _ERROR_LINE not in _log(stack, "backend")

    # A respawned worker booted under the edited value: it lists nothing on the next reload.
    stack.restart("backend")
    report = await api.post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)
    awaiting = _awaiting(report)
    kinds = _kinds(stack)
    for name, keys in awaiting.items():
        expected = [] if kinds[name][0] == "backend" else [_RECYCLE_VAR]
        assert keys == expected, f"{name}: {keys} (census {kinds})"
    serve_lines = [line for line in _log(stack, "serve").splitlines() if _ERROR_LINE in line]
    assert len(serve_lines) == 2, serve_lines
    for name, (kind, _generation) in kinds.items():
        assert (f"{name} ({_RECYCLE_VAR})" in serve_lines[1]) is (kind != "backend"), serve_lines[1]
