"""The schedule door: ``fire_schedule_door`` (one mechanism) and ``backend_fire`` (the worker seam).

Every scheduled/forwarded fire drives the shared ``visit`` through ``fire_schedule_door``; a plain job
with no door signal runs ``run_tool`` unchanged. The fire's subject context is deposited around the
run and the door-layer binding is handed to ``visit`` (never an ambient deposit).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.interactions import ParkedEntry, VisitOutcome
from tai42_contract.interactions.door_contract import ParkableDoorMixin
from tai42_contract.states import StateSubject
from tai42_contract.template import TemplatedText

from tai42_kit.backend import CallbackSchema, backend_fire, callback_execution, fire_schedule_door
from tai42_kit.utils.schedule_subject import (
    SCHEDULE_CONTRACT_ARG,
    SCHEDULE_EXECUTION_FINGERPRINT_ARG,
    SCHEDULE_EXECUTION_KEY_ARG,
    SCHEDULE_SUBJECT_ARG,
)
from tai42_kit.utils.state_context import current_state_context

_SUBJECT = {"target_kind": "tool", "target_name": "assistant", "kind": "person", "key": "p-1"}


class _FakeResourceManager:
    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        assert text.content is not None
        return text.content


class _FakeTools:
    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []

    async def run_tool(self, key: str, arguments: Any, *, offload_sync: bool = False, extras: Any = None) -> Any:
        self.calls.append(
            SimpleNamespace(
                key=key,
                arguments=dict(arguments),
                offload_sync=offload_sync,
                extras=dict(extras or {}),
                context=current_state_context(),
            )
        )
        return {"ran": key}


class _FakeInteractions:
    def __init__(self) -> None:
        self.visit_calls: list[SimpleNamespace] = []
        self.binds: list[tuple[str, str]] = []
        self.parked_context: Any = None
        self.fire_identity: tuple[str, str] | None = None

    def current_fire_identity(self) -> tuple[str, str] | None:
        return self.fire_identity

    async def list_parked_for(self, context: Any) -> list[Any]:
        self.parked_context = context
        return []

    async def visit(
        self,
        *,
        target_name: str,
        cancel: list[str],
        resume: list[Any],
        start: Any,
        extras: Any,
        state_binding: Any,
        receives_outcome: bool,
    ) -> VisitOutcome:
        result = await start(extras) if start is not None else None
        self.visit_calls.append(
            SimpleNamespace(
                target_name=target_name,
                cancel=cancel,
                resume=resume,
                started=start is not None,
                extras=dict(extras),
                state_binding=state_binding,
                receives_outcome=receives_outcome,
            )
        )
        return VisitOutcome(
            action="started" if start is not None else "none",
            kind="result" if start is not None else "none",
            result=result,
        )

    def bound_execution_identity_for_fire(self, user_id: str, fingerprint: str) -> Any:
        self.binds.append((user_id, fingerprint))

        @asynccontextmanager
        async def _cm() -> AsyncIterator[None]:
            yield

        return _cm()


class _FakeApp:
    def __init__(self) -> None:
        self.storage = SimpleNamespace(resource_manager=_FakeResourceManager())
        self.tools = _FakeTools()
        self.interactions = _FakeInteractions()


@pytest.fixture
def app() -> Iterator[_FakeApp]:
    fake = _FakeApp()
    with tai42_app.bound(fake):
        yield fake


async def test_fire_schedule_door_starts_the_tool_through_visit(app: _FakeApp) -> None:
    subject = StateSubject.model_validate(_SUBJECT)
    outcome = await fire_schedule_door(
        "greet", {"m": "hi"}, subject=subject, state_binding=None, contract=None, receives_outcome=True
    )
    assert outcome.kind == "result"
    call = app.interactions.visit_calls[0]
    assert call.target_name == "greet"
    assert call.receives_outcome is True
    # The base arguments are dispatched, under the deposited schedule subject context.
    run = app.tools.calls[0]
    assert run.key == "greet"
    assert run.arguments == {"m": "hi"}
    assert run.context is not None
    assert run.context.door == "schedule"
    assert run.context.candidates.by_kind == {"person": "p-1"}


async def test_fire_schedule_door_start_expr_replaces_kwargs(app: _FakeApp) -> None:
    subject = StateSubject.model_validate(_SUBJECT)
    contract = ParkableDoorMixin(start_expr=TemplatedText(content="{greeting: .m}"))
    await fire_schedule_door(
        "greet", {"m": "hi"}, subject=subject, state_binding=None, contract=contract, receives_outcome=False
    )
    assert app.tools.calls[0].arguments == {"greeting": "hi"}
    assert app.interactions.visit_calls[0].receives_outcome is False


async def test_fire_schedule_door_binds_parked_with_no_null_keys(app: _FakeApp) -> None:
    # A parked entry's unset optional fields are ABSENT from ``$parked``, never null-valued keys:
    # the door binds the one compact shape.
    async def _parked(context: Any) -> list[Any]:
        return [ParkedEntry(id="i-1", status="asking")]

    app.interactions.list_parked_for = _parked  # type: ignore[method-assign]
    subject = StateSubject.model_validate(_SUBJECT)
    contract = ParkableDoorMixin(
        start_expr=TemplatedText(content="{nulls: [$parked[0] | to_entries[] | select(.value == null) | .key]}")
    )
    await fire_schedule_door(
        "greet", {"m": "hi"}, subject=subject, state_binding=None, contract=contract, receives_outcome=False
    )
    assert app.tools.calls[0].arguments == {"nulls": []}


async def test_backend_fire_plain_job_runs_run_tool_directly(app: _FakeApp) -> None:
    result = await backend_fire("greet", {"m": "hi"})
    assert result == {"ran": "greet"}
    assert app.tools.calls[0].key == "greet"
    # A plain job drives no visit — it is today's run_tool, byte-for-byte.
    assert app.interactions.visit_calls == []


async def test_backend_fire_forwarded_subject_drives_the_door(app: _FakeApp) -> None:
    kwargs = {SCHEDULE_SUBJECT_ARG: _SUBJECT, "m": "hi"}
    await backend_fire("greet", kwargs)
    assert app.interactions.visit_calls[0].receives_outcome is False
    assert app.tools.calls[0].context.door == "schedule"
    # The reserved kwarg is popped — it never reaches the tool.
    assert app.tools.calls[0].arguments == {"m": "hi"}
    assert app.interactions.binds == []


async def test_backend_fire_with_identity_binds_then_drives(app: _FakeApp) -> None:
    kwargs = {
        SCHEDULE_SUBJECT_ARG: _SUBJECT,
        SCHEDULE_EXECUTION_KEY_ARG: "svc",
        SCHEDULE_EXECUTION_FINGERPRINT_ARG: "fp-1",
        SCHEDULE_CONTRACT_ARG: {"start_expr": {"content": "{greeting: .m}"}},
        "m": "hi",
    }
    await backend_fire("greet", kwargs)
    assert app.interactions.binds == [("svc", "fp-1")]
    assert app.tools.calls[0].arguments == {"greeting": "hi"}
    assert app.tools.calls[0].context.door == "schedule"


async def test_callback_with_no_forward_runs_plainly(app: _FakeApp) -> None:
    await callback_execution({"r": 1}, CallbackSchema(tool="follow"))
    assert app.tools.calls[0].key == "follow"
    # No forwarded door context → a plain follow-up, no visit and no identity bind.
    assert app.interactions.visit_calls == []
    assert app.interactions.binds == []


async def test_callback_with_forwarded_pair_binds_and_subject_tracks(app: _FakeApp) -> None:
    callback = CallbackSchema(
        tool="follow",
        carried_kwargs={
            SCHEDULE_SUBJECT_ARG: _SUBJECT,
            SCHEDULE_EXECUTION_KEY_ARG: "svc",
            SCHEDULE_EXECUTION_FINGERPRINT_ARG: "fp-1",
        },
    )
    await callback_execution({"r": 1}, callback)
    # The followed run's identity is re-bound and its subject context re-established; the follow-up
    # drives through the receiver-less visit, so a follow-up that async-asks is subject-tracked.
    assert app.interactions.binds == [("svc", "fp-1")]
    assert app.interactions.visit_calls[0].receives_outcome is False
    assert app.tools.calls[0].context.door == "schedule"


async def test_prepare_backend_kwargs_forwards_ambient_subject_and_identity(app: _FakeApp) -> None:
    from tai42_contract.states import StateContext, SubjectCandidates

    from tai42_kit.backend import prepare_backend_kwargs
    from tai42_kit.utils.state_context import state_context

    app.interactions.fire_identity = ("svc", "fp-1")

    async def some_tool(a: int) -> None: ...

    ctx = StateContext(
        door="schedule",
        candidates=SubjectCandidates(target_kind="tool", target_name="assistant", by_kind={"person": "p-1"}),
        actor=None,
    )
    with state_context(ctx):
        kwargs = await prepare_backend_kwargs(some_tool, "backend_tool_name", "some_tool", {"a": 1})
    assert StateSubject.model_validate(kwargs[SCHEDULE_SUBJECT_ARG]) == StateSubject.model_validate(_SUBJECT)
    assert kwargs[SCHEDULE_EXECUTION_KEY_ARG] == "svc"
    assert kwargs[SCHEDULE_EXECUTION_FINGERPRINT_ARG] == "fp-1"


async def test_prepare_backend_kwargs_forwards_nothing_without_ambient_context(app: _FakeApp) -> None:
    from tai42_kit.backend import prepare_backend_kwargs

    app.interactions.fire_identity = ("svc", "fp-1")

    async def some_tool(a: int) -> None: ...

    kwargs = await prepare_backend_kwargs(some_tool, "backend_tool_name", "some_tool", {"a": 1})
    assert SCHEDULE_SUBJECT_ARG not in kwargs
    assert SCHEDULE_EXECUTION_KEY_ARG not in kwargs
