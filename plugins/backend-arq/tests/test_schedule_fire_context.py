"""A scheduled arq fire runs the tool inside a ``schedule`` state context built from the
job's stamped subject; a plain background run (no stamped subject) runs context-free (its
``api`` door is stamped at the platform write chokepoint)."""

from __future__ import annotations

from typing import Any

from tai42_contract.states import StateContext
from tai42_kit.utils.schedule_subject import SCHEDULE_SUBJECT_ARG
from tai42_kit.utils.state_context import current_state_context

from tai42_backend_arq import tasks
from tai42_backend_arq.settings import arq_settings

_SUBJECT = {"target_kind": "tool", "target_name": "assistant", "kind": "person", "key": "p-1"}
_CTX: dict[str, Any] = {"redis": None, "job_id": "job-1"}


def _capture(seen: list[StateContext | None]):
    async def _run_tool(key: str, arguments: dict[str, Any], *, offload_sync: bool = False) -> Any:
        seen.append(current_state_context())
        return "ok"

    return _run_tool


async def test_scheduled_fire_runs_in_the_schedule_context(stub_app, monkeypatch) -> None:
    seen: list[StateContext | None] = []
    monkeypatch.setattr(stub_app.tools, "run_tool", _capture(seen))
    arg = arq_settings().tool_name_arg
    await tasks.tool_execution(_CTX, **{arg: "greet", SCHEDULE_SUBJECT_ARG: _SUBJECT, "message": "hi"})
    assert len(seen) == 1
    ctx = seen[0]
    assert ctx is not None
    assert ctx.door == "schedule"
    assert ctx.actor is None
    assert ctx.candidates.by_kind == {"person": "p-1"}
    assert current_state_context() is None


async def test_background_run_carries_no_context_even_with_a_top_level_subject(stub_app, monkeypatch) -> None:
    seen: list[StateContext | None] = []
    monkeypatch.setattr(stub_app.tools, "run_tool", _capture(seen))
    arg = arq_settings().tool_name_arg
    await tasks.tool_execution(_CTX, **{arg: "greet", "subject": _SUBJECT, "message": "hi"})
    assert seen == [None]
