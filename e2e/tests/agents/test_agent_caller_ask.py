"""The SSE agent-run door under the run-delivery bind: an async caller ask ends the stream with the
ASKS terminal frame, a finishing run ends it with the FINAL frame.

An agent run started at ``POST /api/agents/{name}/runs`` drives inside the shared ``visit`` under a
PUSH ``name=agent`` frame that mints the run's delivery id — so a ``to="caller"`` async ask parks
with a captured ``run_delivery_id`` (a park that stored none raises), and the stream ends with the
``asks_final`` frame carrying the caller ask entries rather than a synchronous answer. A run that
finishes streams its chunks and ends with the ``message_final`` frame.

Both cases run over ``agent_route_park_stack`` — the one durable-checkpoint SSE stack the caller-ask
probe agent is registered on. A ``to="caller"`` ask needs an authenticated caller, so this leg
requires the auth-ON stack; the finishing leg is auth-agnostic, so it shares the same stack rather
than booting a second checkpoint-bearing module stack in the session (one infra instance carries a
single checkpoint-redis DB slot). The nested-driver re-entry cases (an agent running a tool of
another driver that parks on a USER ask and re-enters the agent on the answer) need that driver,
which is not part of the e2e fixtures — the platform side (the SSE ASKS/FINAL terminals and the
caller-ask park) is covered here; the re-entry is proven in that driver's own suite.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import urlencode

import httpx
import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
    ),
]

_PARK_EXPIRY_SECONDS = 3600
_DOOR_AGENT = "e2e_door_agent"


def _terminal(frames: list[dict]) -> dict:
    """The run's terminal frame — the last frame before the ``stream.end`` marker."""
    body = [f for f in frames if f.get("type") != "stream.end"]
    assert body, f"the SSE run produced no terminal frame: {frames}"
    return body[-1]


async def _run_sse(
    stack: TaiStack, name: str, body: dict, *, token: str | None = None, subject: dict[str, str] | None = None
) -> list[dict]:
    """POST an agent run and collect its SSE frames, parsed from each ``data:`` line.

    ``subject`` names the run's async-park subject on the query the door reads it off
    (``subject_kind`` / ``subject_key`` / ``subject_target``) — a ``to="caller"`` ask needs one.
    """
    url = f"http://{stack.host}:{stack.port_a}/api/agents/{name}/runs"
    if subject is not None:
        query = urlencode({f"subject_{field}": value for field, value in subject.items()})
        url = f"{url}?{query}"
    headers = {"Authorization": f"Bearer {token}"} if token is not None else None
    frames: list[dict] = []
    async with (
        httpx.AsyncClient(timeout=30.0) as client,
        client.stream("POST", url, json=body, headers=headers) as response,
    ):
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                frames.append(json.loads(line[len("data:") :].strip()))
    return frames


async def test_sse_run_that_async_asks_its_caller_ends_with_the_asks_frame(
    agent_route_park_stack: tuple[TaiStack, str], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    # A caller ask needs an authenticated caller, so this runs on the auth-ON SSE stack.
    stack, root_token = agent_route_park_stack
    question = uniq("sse-question")
    llm_stub.reset()
    llm_stub.script(
        [
            {
                "tool_call": {
                    "name": "e2e_caller_ask",
                    "arguments": {"question": question, "expiry_seconds": _PARK_EXPIRY_SECONDS},
                }
            }
        ]
    )

    # A ``to="caller"`` ask indexes its park on the run's named subject, so the door names one on
    # the query the same way every direct door does; without it the ask would refuse pre-persist.
    subject = {"kind": "job", "key": uniq("sse-subj"), "target": _DOOR_AGENT}
    frames = await _run_sse(
        stack, _DOOR_AGENT, {"user_message": {"content": uniq("sse-open")}}, token=root_token, subject=subject
    )

    # The run's terminal is the ASKS frame carrying the caller ask (never a synchronous answer); the
    # park's delivery id was minted (a park with none would have raised). ``stream.end`` closes it.
    assert frames[-1]["type"] == "stream.end", frames
    terminal = _terminal(frames)
    assert terminal["type"] == "asks_final", terminal
    assert question in json.dumps(terminal["asks"]), terminal
    assert any(a.get("to") == "caller" for a in terminal["asks"]), terminal


async def test_sse_run_that_finishes_ends_with_the_final_frame(
    agent_route_park_stack: tuple[TaiStack, str], llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack, root_token = agent_route_park_stack
    final = uniq("sse-final")
    llm_stub.reset()
    llm_stub.script([{"content": final}])

    frames = await _run_sse(stack, _DOOR_AGENT, {"user_message": {"content": uniq("sse-open")}}, token=root_token)

    # A finishing run streams its chunks and its terminal is the FINAL frame carrying the answer.
    assert frames[-1]["type"] == "stream.end", frames
    terminal = _terminal(frames)
    assert terminal["type"] == "message_final", terminal
    assert final in json.dumps(terminal), terminal
