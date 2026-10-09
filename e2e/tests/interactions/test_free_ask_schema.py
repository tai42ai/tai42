"""A ``free`` caller ask given a ``schema`` checks the resuming answer against it.

``held_run`` parks a ``free`` caller ask whose answer must be an integer. A later run on the same
subject resuming it with a string is refused with the schema mismatch and the ask stays parked;
resuming it with an integer resolves it and the held run finishes with that answer.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.stack import TaiStack

from ._caller_support import await_status, caller_ask_id, subject, submit

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs("kind:tool_runs", "kind:interactions", "probe-tools", "topology:replicas"),
]

_INTEGER = {"type": "integer"}


async def test_a_resume_outside_the_free_schema_is_refused_and_a_conforming_one_resolves(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    subj = subject(uniq("subject"))

    held = await submit(api_a, "held_run", {"marker": uniq("q"), "answer_format": "free", "schema": _INTEGER}, subj)
    await await_status(api_b, held, "parked")
    ask_id = await caller_ask_id(api_a, subj)

    refused = await submit(api_b, "caller_relay", {"payload": "not an integer"}, subj)
    refused_view = await await_status(api_a, refused, "failed")
    assert "answer does not match schema: 'not an integer' is not of type 'integer'" in refused_view["error"]
    # The refused resume consumed nothing: the same ask is still parked.
    assert await caller_ask_id(api_b, subj) == ask_id

    resolved = await submit(api_b, "caller_relay", {"payload": 7}, subj)
    outcome = (await await_status(api_a, resolved, "succeeded"))["result"]
    assert outcome["action"] == "resumed"
    assert outcome["result"]["answer"] == 7
