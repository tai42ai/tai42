"""A job holding only the arguments its caller supplied runs the named tool with the tool's own defaults.

The dispatching branches leave an omitted optional argument out of the job kwargs; the worker
then runs the tool without it, after the kwargs crossed Celery's task serializer.
"""

from __future__ import annotations

from typing import Any

from kombu.utils.json import dumps, loads

from tai42_backend_celery.core.tasks import tool_execution

_SHIPPED = {"text": "x", "backend_tool_name": "probe", "backend_secret_capability": False}


def test_the_worker_runs_the_named_tool_without_the_omitted_argument(stub_app: Any) -> None:
    stub_app.tools.run_tool_result = "ran"
    try:
        out = tool_execution.apply(kwargs=loads(dumps(_SHIPPED))).get()
    finally:
        tool_execution.close_loop()
    assert out == "ran"
    assert stub_app.tools.run_tool_calls == [("probe", {"text": "x"})]
