"""Fixture tools for the schedule-create translation test.

``demo_report`` records every invocation, so a guard that must never reach the base
tool is proved by an empty record. ``plain_task`` has no schedule branch, so
scheduling it on a cadence raises the missing-extension error. The two ``backend_*``
tools are the scheduling-availability markers the door pre-checks.
"""

from tai42_contract.app import tai42_app

demo_calls: list[dict] = []


@tai42_app.tools.tool
def demo_report(payload: str = "hi") -> str:
    """Record the call and echo ``payload``."""
    demo_calls.append({"payload": payload})
    return payload


@tai42_app.tools.tool
def plain_task(payload: str = "hi") -> str:
    """A tool with no schedule branch attached."""
    return payload


@tai42_app.tools.tool
def backend_list_schedules() -> list:
    """Scheduling-availability marker."""
    return []


@tai42_app.tools.tool
def backend_delete_schedule(name: str) -> dict:
    """Scheduling-availability marker."""
    return {"removed": name}
