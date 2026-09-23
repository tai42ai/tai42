"""The celery worker fires every dequeued job through the kit ``backend_fire`` seam.

A job carrying a stamped firing identity (a contract-bearing schedule, or a background task that
forwarded the ambient subject/identity) binds that identity and drives the schedule door; a plain
job carrying no door signal runs the tool directly, context-free (its ``api`` door is stamped at the
platform write chokepoint)."""

from __future__ import annotations

from tai42_kit.utils.schedule_subject import (
    SCHEDULE_CONTRACT_ARG,
    SCHEDULE_EXECUTION_FINGERPRINT_ARG,
    SCHEDULE_EXECUTION_KEY_ARG,
    SCHEDULE_SUBJECT_ARG,
)

from tai42_backend_celery.core.settings import celery_settings
from tai42_backend_celery.core.tasks import run_tool

_SUBJECT = {"target_kind": "tool", "target_name": "assistant", "kind": "person", "key": "p-1"}


async def test_scheduled_fire_with_key_and_contract_drives_the_door(stub_app) -> None:
    stub_app.tools.run_tool_result = "ok"
    arg = celery_settings().tool_name_arg
    await run_tool(
        **{
            arg: "greet",
            SCHEDULE_SUBJECT_ARG: _SUBJECT,
            SCHEDULE_EXECUTION_KEY_ARG: "svc",
            SCHEDULE_EXECUTION_FINGERPRINT_ARG: "fp-1",
            SCHEDULE_CONTRACT_ARG: {"start_expr": {"content": "{greeting: .m}"}},
            "m": "hi",
        }
    )
    # The stamped identity is bound and the door is driven receiver-less; the contract's ``start_expr``
    # replaces the base arguments, and the tool runs under the ``schedule`` subject context.
    assert stub_app.interactions.binds == [("svc", "fp-1")]
    assert stub_app.interactions.visit_calls[0].receives_outcome is False
    assert stub_app.interactions.visit_calls[0].context.door == "schedule"
    assert stub_app.interactions.visit_calls[0].context.candidates.by_kind == {"person": "p-1"}
    assert stub_app.tools.run_tool_calls == [("greet", {"greeting": "hi"})]


async def test_task_job_with_forwarded_subject_drives_the_door(stub_app) -> None:
    stub_app.tools.run_tool_result = "ok"
    arg = celery_settings().tool_name_arg
    await run_tool(
        **{
            arg: "greet",
            SCHEDULE_SUBJECT_ARG: _SUBJECT,
            SCHEDULE_EXECUTION_KEY_ARG: "svc",
            SCHEDULE_EXECUTION_FINGERPRINT_ARG: "fp-1",
            "message": "hi",
        }
    )
    # A forwarded pair with no contract drives the empty door: the base arguments start unchanged under
    # the bound identity and the ``schedule`` subject context.
    assert stub_app.interactions.binds == [("svc", "fp-1")]
    assert stub_app.interactions.visit_calls[0].receives_outcome is False
    assert stub_app.interactions.visit_calls[0].context.door == "schedule"
    assert stub_app.tools.run_tool_calls == [("greet", {"message": "hi"})]


async def test_plain_background_job_runs_the_tool_directly(stub_app) -> None:
    stub_app.tools.run_tool_result = "ok"
    arg = celery_settings().tool_name_arg
    # A top-level ``subject`` argument with NO stamped door signal is an ordinary tool argument: the run
    # stays a plain ``run_tool`` with no door drive (its ``api`` door is stamped at the write chokepoint).
    await run_tool(**{arg: "greet", "subject": _SUBJECT, "message": "hi"})
    assert stub_app.interactions.visit_calls == []
    assert stub_app.interactions.binds == []
    assert stub_app.tools.run_tool_calls == [("greet", {"subject": _SUBJECT, "message": "hi"})]
