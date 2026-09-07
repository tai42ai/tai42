"""The platform's own state-consumer listers (hooks / schedules / agents).

Each family's matcher is pinned as a pure function (registry data in, rows out): a match,
a non-match, and — for schedules — the no-backend ``unavailable`` row. The lister wiring
(existence gate + the export-surface read) is pinned through the module seams so no live
process app is needed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field
from tai42_contract.hooks import HookParams, HookSubject
from tai42_contract.presets import PresetBody
from tai42_contract.states.models import StateSubject

from tai42_skeleton.operations import NotSupportedError
from tai42_skeleton.states import consumers as consumers_mod
from tai42_skeleton.states.consumers import (
    agent_consumer_rows,
    hook_consumer_rows,
    preset_consumer_rows,
    schedule_consumer_rows,
    schedules_lister,
)


def _hook(name: str, *, subject_kind: str | None) -> HookParams:
    subject = (
        None
        if subject_kind is None
        else HookSubject(target_kind="agent", target_name="a", kind=subject_kind, key_expr=".id")
    )
    return HookParams(
        name=name,
        topic="alerts",
        tool="notify",
        execution_key="k-1",
        execution_key_fingerprint="fp-1",
        subject=subject,
    )


def test_hook_rows_match_only_a_declared_subject_kind() -> None:
    hooks = [
        _hook("on-person", subject_kind="person"),
        _hook("on-thread", subject_kind="thread"),
        _hook("no-subject", subject_kind=None),
    ]
    rows = hook_consumer_rows(hooks, {"person"})
    assert [(r.kind, r.name, r.detail) for r in rows] == [("hook", "on-person", "supplies kind person")]
    link = rows[0].link
    assert link is not None
    assert link.token == "hooks"


def test_hook_rows_are_empty_when_no_kind_matches() -> None:
    assert hook_consumer_rows([_hook("on-thread", subject_kind="thread")], {"person"}) == []


def _schedule_record(name: str, subject: StateSubject | None) -> dict:
    from tai42_kit.utils.schedule_subject import SCHEDULE_SUBJECT_ARG

    kwargs: dict = {"to": "x"}
    if subject is not None:
        kwargs[SCHEDULE_SUBJECT_ARG] = subject.model_dump()
    return {"name": name, "args": [], "kwargs": kwargs, "schedule": {}, "enabled": True}


def test_schedule_rows_match_a_declared_subject_kind() -> None:
    subject = StateSubject(target_kind="agent", target_name="a", kind="person", key="p-1")
    records = [
        _schedule_record("nightly", subject),
        _schedule_record("no-subject", None),
    ]
    rows = schedule_consumer_rows(records, {"person"})
    assert [(r.kind, r.name, r.detail) for r in rows] == [("schedule", "nightly", "carries subject kind person")]
    link = rows[0].link
    assert link is not None
    assert link.token == "scheduling"


def test_schedule_rows_skip_an_undeclared_subject_kind() -> None:
    subject = StateSubject(target_kind="agent", target_name="a", kind="thread", key="t-1")
    assert schedule_consumer_rows([_schedule_record("nightly", subject)], {"person"}) == []


def test_schedules_lister_returns_a_muted_row_with_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _kinds(_state: str) -> set[str]:
        return {"person"}

    async def _raise() -> object:
        raise NotSupportedError("no installed backend exposes scheduling tools")

    monkeypatch.setattr(consumers_mod, "_declared_subject_kinds", _kinds)
    monkeypatch.setattr("tai42_skeleton.operations.schedules.export_schedules_raw", _raise)
    rows = list(_run(schedules_lister("alerts")))
    assert len(rows) == 1
    assert rows[0].kind == "schedule"
    assert rows[0].unavailable == "no scheduling backend"
    assert rows[0].name is None


class _StateAgentInput(BaseModel):
    tool_names: list[str] = Field(default_factory=lambda: ["state_replace", "other"])


class _PlainAgentInput(BaseModel):
    prompt: str = ""


def test_agent_rows_match_an_agent_that_binds_a_state_builtin() -> None:
    agents = {
        "writer": SimpleNamespace(tool_names=["state_read", "echo"]),
        "reader": SimpleNamespace(tool_names=["echo", "search"]),
    }
    rows = agent_consumer_rows(agents)  # type: ignore[arg-type]
    assert [(r.kind, r.name, r.detail) for r in rows] == [("agent", "writer", "uses state_read")]
    link = rows[0].link
    assert link is not None
    assert link.token == "agents"


def test_agent_rows_read_tool_names_from_the_tool_input_default() -> None:
    # An agent that declares no ``tool_names`` attribute but bakes them into its ToolInput
    # default is still matched (the fallback read), while a plain agent names none.
    agents = {
        "baked": SimpleNamespace(ToolInput=_StateAgentInput),
        "plain": SimpleNamespace(ToolInput=_PlainAgentInput),
    }
    rows = agent_consumer_rows(agents)  # type: ignore[arg-type]
    assert [(r.kind, r.name, r.detail) for r in rows] == [("agent", "baked", "uses state_replace")]


def _preset(base_tool: str, tool_names: object) -> PresetBody:
    fixed_kwargs: dict = {} if tool_names is None else {"tool_names": tool_names}
    return PresetBody(base_tool=base_tool, description="d", fixed_kwargs=fixed_kwargs)


def test_preset_rows_match_a_preset_that_bakes_a_state_builtin() -> None:
    bodies = {
        "assistant": _preset("run_agent", ["state_merge", "echo", "state_read"]),
        "reader": _preset("run_agent", ["echo", "search"]),
    }
    rows = preset_consumer_rows(bodies)
    # Only the state builtins land in the detail, sorted; the non-state preset drops out.
    assert [(r.kind, r.name, r.detail) for r in rows] == [("preset", "assistant", "binds state_merge, state_read")]
    link = rows[0].link
    assert link is not None
    assert link.token == "presets"


def test_preset_rows_skip_a_preset_naming_a_non_state_tool() -> None:
    assert preset_consumer_rows({"reader": _preset("run_agent", ["echo", "search"])}) == []


def test_preset_rows_skip_a_preset_with_no_baked_tool_names() -> None:
    # A preset that bakes no ``tool_names`` (a non-agent base, or an agent left to resolve
    # its tools at run time) binds no state tool statically — no row.
    bodies = {
        "no-kwargs": _preset("run_agent", None),
        "not-a-list": _preset("run_agent", "state_read"),
    }
    assert preset_consumer_rows(bodies) == []


def _run(coro):
    import asyncio

    return asyncio.run(coro)
