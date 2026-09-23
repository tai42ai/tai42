"""Agents router tests — the ``/api/agents`` list door and the
``/api/agents/{name}/runs`` SSE run door.

Handlers are driven directly (the router-test pattern): a FAKE agent implementing
the contract ``Agent`` ABC yields a scripted event sequence, and the module's
agent registry seam is monkeypatched to expose it. No concrete agent
implementation is referenced — the surface binds to the contract only.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from tai42_contract.agent.events import AsksFinal, InterruptFinal, StructuredFinal, SuspendedFinal
from tai42_contract.interactions import ParkedEntry
from tai42_contract.tools import current_call_chain, get_run_delivery_id

from tai42_skeleton.agent import (
    Agent,
    MessageDelta,
    MessageFinal,
    ReasoningStep,
    RunUsage,
    ToolCallStep,
    ToolResultStep,
)
from tai42_skeleton.operations import agents as agent_ops
from tai42_skeleton.routers import agents as router
from tai42_skeleton.routers import interactions as interactions_router
from tai42_skeleton.tools.adapters.lc_tool_to_func import build_signature


class _NonJsonPayload:
    """A live, non-JSON-native object of the kind a tool result carries (e.g. a
    ``ToolMessage.content``). It has no pydantic serializer, so a bare
    ``model_dump_json`` would raise — ``fallback=str`` renders it via ``str``."""

    def __init__(self, label: str) -> None:
        self.label = label

    def __str__(self) -> str:
        return f"non-json:{self.label}"


class _FakeInput(BaseModel):
    prompt: str
    count: int = 1


class _FakeAgent(Agent):
    """A streaming agent that replays a scripted event sequence. It records the
    kwargs its ``astream`` received (to pin the ``from_tool_input`` mapping) and
    sets ``cancelled`` when a disconnect cancels it mid-run."""

    tool_name = "faker"
    tool_description = "A fake streaming agent."
    ToolInput = _FakeInput

    def __init__(
        self,
        events: list[Any] | None = None,
        *,
        raise_after: Exception | None = None,
        block: bool = False,
    ) -> None:
        self._events = events or []
        self._raise_after = raise_after
        self._block = block
        self.cancelled = False
        self.received_kwargs: dict[str, Any] | None = None

    async def run(self, **kwargs: Any) -> Any:
        return await self._drain(self.astream(**kwargs))

    async def astream(self, **kwargs: Any):  # type: ignore[override]
        self.received_kwargs = kwargs
        try:
            for event in self._events:
                yield event
            if self._raise_after is not None:
                raise self._raise_after
            if self._block:
                # Park until cancelled — models an agent still working when the
                # client disconnects.
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _RenamingAgent(_FakeAgent):
    """Overrides ``from_tool_input`` to rename a field, so a raw pass-through
    would send the wrong kwargs — pins that the route maps through it."""

    @classmethod
    def from_tool_input(cls, validated: BaseModel) -> dict[str, Any]:
        data = {name: getattr(validated, name) for name in validated.model_fields_set}
        if "prompt" in data:
            data["user_message"] = data.pop("prompt")
        return data


class _ConfigInput(BaseModel):
    """Carries the LangGraph configs a run's ``thread_id``/``checkpoint_id`` ride in:
    the plain one plus a voting agent's judge/voter pair."""

    prompt: str = "hi"
    langgraph_config: dict[str, Any] = {}
    judge_langgraph_config: dict[str, Any] = {}
    voter_langgraph_config: dict[str, Any] = {}


class _ConfigAgent(_FakeAgent):
    """Maps every config field straight through to a run kwarg, which is what the
    bridge reservation guard scans."""

    ToolInput = _ConfigInput

    @classmethod
    def from_tool_input(cls, validated: BaseModel) -> dict[str, Any]:
        return {name: getattr(validated, name) for name in validated.model_fields_set}


# -- request builders --------------------------------------------------------


def _make_get_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/agents",
        "headers": [],
        "query_string": b"",
        "client": ("1.2.3.4", 1),
        "path_params": {},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _make_run_request(name: str, body: bytes, *, disconnect: bool = False) -> Request:
    scripted: list[dict] = [{"type": "http.request", "body": body, "more_body": False}]
    if disconnect:
        scripted.append({"type": "http.disconnect"})
    idx = {"i": 0}

    async def receive():
        i = idx["i"]
        if i < len(scripted):
            idx["i"] += 1
            return scripted[i]
        # Past the scripted messages: a disconnected client keeps reporting
        # disconnect; a live one reports a benign (non-disconnect) frame so the
        # monitor's ``is_disconnected`` stays False.
        return {"type": "http.disconnect"} if disconnect else {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/api/agents/{name}/runs",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "client": ("1.2.3.4", 1),
        "path_params": {"name": name},
    }
    return Request(scope, receive)


async def _collect(response: Response) -> list[str]:
    assert isinstance(response, StreamingResponse)
    out: list[str] = []
    async for chunk in response.body_iterator:
        out.append(chunk if isinstance(chunk, str) else bytes(chunk).decode())
    return out


def _data_frames(frames: list[str]) -> list[dict]:
    """Parse the JSON ``data:`` payloads (dropping keep-alive comments)."""
    out: list[dict] = []
    for frame in frames:
        if frame.startswith(":"):
            continue
        assert frame.startswith("data: "), frame
        out.append(json.loads(frame[len("data: ") :].strip()))
    return out


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    # One test derives its reference headers from the interactions stream route, which
    # answers 501 (not a StreamingResponse) when the interactions store is OFF;
    # configure it so the reference is the live streaming response.
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def one_agent(monkeypatch):
    """Register a single fake agent under ``faker`` on the router's registry
    seam and return a factory that swaps the agent's scripted behavior."""
    holder: dict[str, Agent] = {}

    def _install(agent: Agent) -> Agent:
        holder["faker"] = agent
        return agent

    # The registry seam lives in ``operations.agents``: the list ops call it
    # there, and the still-handler SSE run doors reach it through the same op
    # module — so one patch drives both surfaces.
    monkeypatch.setattr(agent_ops, "_agents_registry", lambda: dict(holder))
    return _install


# -- list route --------------------------------------------------------------


async def test_list_shape_and_schema_equals_run_tool_schema(one_agent):
    agent = one_agent(_FakeAgent())
    resp = await router.list_agents(_make_get_request())
    payload = json.loads(bytes(resp.body))
    assert payload["data"]["total"] == 1
    item = payload["data"]["items"][0]
    assert item["name"] == "faker"
    assert item["description"] == "A fake streaming agent."
    assert item["tool_name"] == "faker"
    # One schema source: the binding synthesizes the run tool from this exact
    # ``ToolInput`` model via ``build_signature`` (the same call the binding
    # makes), and the route publishes that same model's schema. Assert the
    # route's schema against the model read from the agent, and that the binding's
    # own signature derivation carries the identical field contract (names +
    # required set) — so a route that diverged from the model the binding builds
    # the tool from is caught.
    input_model = agent.ToolInput
    assert item["input_schema"] == input_model.model_json_schema()
    sig = build_signature(input_model, return_annotation=Any)
    assert set(sig.parameters) == set(item["input_schema"]["properties"])
    required_params = {name for name, p in sig.parameters.items() if p.default is p.empty}
    assert required_params == set(item["input_schema"].get("required", []))


# -- run route: event streaming ----------------------------------------------


async def test_run_streams_full_sequence_in_order_and_parses_back(one_agent):
    events = [
        ReasoningStep(text="thinking"),
        ToolCallStep(tool="search", args={"q": "x"}, call_id="c1"),
        ToolResultStep(tool="search", call_id="c1", result={"hits": 2}),
        MessageDelta(text="hel"),
        MessageDelta(text="lo"),
        RunUsage(input_tokens=3, output_tokens=1, total_tokens=4, model="m"),
        MessageFinal(text="hello"),
    ]
    one_agent(_FakeAgent(events))
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    frames = _data_frames(await _collect(resp))

    # Every scripted event arrives in order and parses back into its contract
    # model; a terminal stream.end settles the run.
    assert [f["type"] for f in frames] == [
        "reasoning_step",
        "tool_call_step",
        "tool_result_step",
        "message_delta",
        "message_delta",
        "run_usage",
        "message_final",
        "stream.end",
    ]
    assert ReasoningStep.model_validate(frames[0]).text == "thinking"
    assert ToolResultStep.model_validate(frames[2]).result == {"hits": 2}
    assert MessageFinal.model_validate(frames[6]).text == "hello"


async def test_run_flushes_connect_comment_before_first_event(one_agent):
    # The FIRST raw frame is a no-op SSE comment flushed at connect, BEFORE the first agent
    # event (which can lag while the agent thinks). It makes the first body byte arrive at
    # connect, so a fetch-reader client whose Fetch resolves a streamed response only on the
    # first body byte (Firefox; Chromium resolves on the headers) sees the stream connected
    # in ~0.1s instead of waiting up to a keepalive interval for the first frame. Mirrors the
    # interactions stream's connect frame.
    one_agent(_FakeAgent([MessageFinal(text="done")]))
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    raw = await _collect(resp)
    assert raw[0] == ": connected\n\n"
    assert raw[0].startswith(":"), "the connect frame must be an ignorable SSE comment"
    # It precedes every data frame: the first real event follows the connect comment.
    assert _data_frames(raw)[0]["type"] == "message_final"


async def test_close_at_connect_frame_leaks_no_tasks(one_agent):
    # Closing the stream AT the connect-frame suspension (a client that drops the instant the
    # stream connects, before any event) must leave NO background task alive. The connect
    # frame is the FIRST suspension point and sits INSIDE the try whose finally cancels the
    # producer + monitor; a yield placed BEFORE the try would let GeneratorExit skip the
    # finally and LEAK those tasks (the live-run regression this pins). We assert on task
    # liveness, not `agent.cancelled`: a producer cancelled before its body runs never enters
    # astream, so the leak is only visible as an undone task.
    agent = _FakeAgent(block=True)  # its astream would park until cancelled
    one_agent(agent)
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    assert isinstance(resp, StreamingResponse)
    # body_iterator is typed as the broader AsyncContentStream; the stream endpoint always
    # returns the async generator, whose __anext__/aclose drive the suspension under test.
    gen = resp.body_iterator
    assert isinstance(gen, AsyncGenerator)
    before = asyncio.all_tasks()
    assert await gen.__anext__() == ": connected\n\n"  # suspended at the connect-frame yield
    spawned = asyncio.all_tasks() - before  # the producer + monitor the stream started
    assert spawned, "the stream should have spawned its producer/monitor tasks"
    await gen.aclose()  # GeneratorExit at that yield -> the finally must cancel + await them
    leaked = [task for task in spawned if not task.done()]
    assert leaked == [], f"closing at the connect frame leaked {len(leaked)} uncancelled task(s)"


async def test_run_serializes_non_json_native_payload(one_agent):
    # A tool-result whose payload is a live non-JSON object serializes via
    # fallback=str instead of crashing the stream.
    events = [ToolResultStep(tool="t", call_id="c1", result=_NonJsonPayload("blob"))]
    one_agent(_FakeAgent(events))
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    frames = _data_frames(await _collect(resp))
    assert frames[0]["type"] == "tool_result_step"
    assert frames[0]["result"] == "non-json:blob"
    assert frames[-1] == {"type": "stream.end"}


async def test_run_terminates_with_stream_end(one_agent):
    one_agent(_FakeAgent([MessageFinal(text="done")]))
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    frames = _data_frames(await _collect(resp))
    assert frames[-1] == {"type": "stream.end"}


async def test_run_applies_from_tool_input_mapping(one_agent):
    agent = _RenamingAgent([MessageFinal(text="ok")])
    one_agent(agent)
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hey","count":2}'))
    await _collect(resp)
    # The route mapped the validated input through from_tool_input: prompt was
    # renamed to user_message, count passed through — not a raw body pass-through.
    assert agent.received_kwargs == {"user_message": "hey", "count": 2}


async def test_run_response_headers_mirror_interactions_stream(one_agent, monkeypatch, fake_client_ctx):
    one_agent(_FakeAgent([MessageFinal(text="ok")]))
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    assert isinstance(resp, StreamingResponse)
    # The agents stream mirrors the interactions stream route's headers rather
    # than re-listing literals: derive the reference from the interactions stream
    # response itself. That route captures its tail cursor on the store connection
    # BEFORE returning the response, so wire the fake so the capture runs offline;
    # then assert the same media type and no-cache / keep-alive / no-buffering values.
    monkeypatch.setattr(interactions_router, "client_ctx", fake_client_ctx)
    reference = await interactions_router.stream(_make_get_request())
    assert isinstance(reference, StreamingResponse)
    assert resp.media_type == reference.media_type == "text/event-stream"
    for header in ("cache-control", "connection", "x-accel-buffering"):
        assert resp.headers[header] == reference.headers[header]
    await _collect(resp)  # drain so the run task finishes


async def test_run_agent_exception_yields_stream_error(one_agent):
    one_agent(_FakeAgent([ReasoningStep(text="oops")], raise_after=RuntimeError("boom")))
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    frames = _data_frames(await _collect(resp))
    assert frames[0]["type"] == "reasoning_step"
    assert frames[-1] == {"type": "stream.error", "message": "boom"}
    # No stream.end after an error — the error frame is the terminal one.
    assert not any(f["type"] == "stream.end" for f in frames)


async def test_run_disconnect_cancels_underlying_run(one_agent):
    agent = _FakeAgent(block=True)
    one_agent(agent)
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}', disconnect=True))
    await _collect(resp)
    # The disconnect monitor fired; the producer was cancelled, propagating
    # cancellation into astream — the abandoned run stopped.
    assert agent.cancelled is True


# -- run route: input / lookup errors ----------------------------------------


async def test_run_invalid_input_400(one_agent):
    one_agent(_FakeAgent())
    # Missing the required ``prompt`` field.
    resp = await router.run_agent(_make_run_request("faker", b'{"count":2}'))
    assert resp.status_code == 400
    assert "invalid agent input" in json.loads(bytes(resp.body))["error"]


async def test_run_invalid_json_body_400(one_agent):
    one_agent(_FakeAgent())
    resp = await router.run_agent(_make_run_request("faker", b"not json"))
    assert resp.status_code == 400
    assert json.loads(bytes(resp.body))["error"] == "invalid JSON body"


async def test_run_non_object_body_400(one_agent):
    one_agent(_FakeAgent())
    resp = await router.run_agent(_make_run_request("faker", b'"scalar"'))
    assert resp.status_code == 400


async def test_run_unknown_agent_404(one_agent):
    one_agent(_FakeAgent())
    resp = await router.run_agent(_make_run_request("ghost", b'{"prompt":"hi"}'))
    assert resp.status_code == 404
    assert "no such agent" in json.loads(bytes(resp.body))["error"]


async def test_run_rejects_unknown_input_field(one_agent):
    # A body key that is not a ``ToolInput`` field is a loud 400 naming it, never a
    # silent drop (pydantic's default ``extra="ignore"``).
    one_agent(_FakeAgent([MessageFinal(text="x")]))
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi","bogus":1}'))
    assert resp.status_code == 400
    assert "unknown agent input field" in json.loads(bytes(resp.body))["error"]
    assert "bogus" in json.loads(bytes(resp.body))["error"]


# -- run route: SSE keep-alive ------------------------------------------------


async def test_run_emits_keepalive_between_idle_events(one_agent, monkeypatch):
    # An idle gap between two events emits a ``: keepalive`` comment frame (so a
    # proxy does not drop the connection) WITHOUT dropping either event at the
    # cancellation boundary — the persistent get-task invariant.
    monkeypatch.setattr(router, "_KEEPALIVE_SECONDS", 0.02)

    class _GatedAgent(_FakeAgent):
        async def astream(self, **kwargs: Any):  # type: ignore[override]
            self.received_kwargs = kwargs
            yield MessageDelta(text="first")
            # Idle longer than the keep-alive cadence so a keepalive frame fires
            # between the two events.
            await asyncio.sleep(0.12)
            yield MessageFinal(text="second")

    one_agent(_GatedAgent())
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    raw = await _collect(resp)

    assert any(frame.startswith(":") and "keepalive" in frame for frame in raw)
    frames = _data_frames(raw)
    assert [f["type"] for f in frames] == ["message_delta", "message_final", "stream.end"]
    assert frames[0]["text"] == "first"
    assert frames[1]["text"] == "second"


# -- run route: from_tool_input ValueError -----------------------------------


class _ConflictAgent(_FakeAgent):
    """``from_tool_input`` raises a plain ``ValueError`` on a conflicting input — the
    loud-400 path distinct from a pydantic ``ValidationError``."""

    @classmethod
    def from_tool_input(cls, validated: BaseModel) -> dict[str, Any]:
        raise ValueError("conflicting fields map to the same run kwarg")


async def test_run_from_tool_input_valueerror_400(one_agent):
    one_agent(_ConflictAgent([MessageFinal(text="ok")]))
    # The input validates against ``ToolInput``, but the mapping step rejects the
    # combination as a loud 400 rather than a silent drop.
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    assert resp.status_code == 400
    assert "invalid agent input" in json.loads(bytes(resp.body))["error"]


# -- run route: reserved bridge: thread namespace ----------------------------

# Any caller config steering a run into the reserved ``bridge:`` thread namespace —
# top-level or per judge/voter, on ``thread_id`` or ``checkpoint_id`` — is a loud 400.
_RESERVED_CONFIGS = [
    ("langgraph_config", "thread_id"),
    ("langgraph_config", "checkpoint_id"),
    ("judge_langgraph_config", "thread_id"),
    ("voter_langgraph_config", "checkpoint_id"),
]


@pytest.mark.parametrize(("config_field", "key"), _RESERVED_CONFIGS)
async def test_run_rejects_caller_supplied_bridge_namespace(one_agent, config_field, key):
    one_agent(_ConfigAgent([MessageFinal(text="x")]))
    body = json.dumps({"prompt": "hi", config_field: {"configurable": {key: "bridge:route:1.2.3.4"}}}).encode()
    resp = await router.run_agent(_make_run_request("faker", body))
    assert resp.status_code == 400
    error = json.loads(bytes(resp.body))["error"]
    assert key in error
    assert "bridge:" in error


async def test_run_allows_a_normal_thread_id(one_agent):
    # A thread_id outside the reserved namespace is not the bridge's to protect — the
    # run streams as usual.
    one_agent(_ConfigAgent([MessageFinal(text="ok")]))
    body = json.dumps({"prompt": "hi", "langgraph_config": {"configurable": {"thread_id": "user-42"}}}).encode()
    resp = await router.run_agent(_make_run_request("faker", body))
    frames = _data_frames(await _collect(resp))
    assert frames[-1] == {"type": "stream.end"}


# -- run route: the shared visit, one terminal frame per outcome kind --------


class _ChainRecordingAgent(_FakeAgent):
    """Records the ambient call chain seen inside its drive, so the door's PUSH frame is observable."""

    def __init__(self, events: list[Any] | None = None) -> None:
        super().__init__(events)
        self.chain: tuple[str, ...] | None = None

    async def astream(self, **kwargs: Any):  # type: ignore[override]
        self.chain = current_call_chain()
        async for event in super().astream(**kwargs):
            yield event


async def test_run_opens_the_push_frame_of_the_target_agent(one_agent):
    # The SSE door opens the outermost minting frame as a PUSH of the target agent's name,
    # so the run drives under a call chain rooted at the agent.
    agent = _ChainRecordingAgent([MessageFinal(text="ok")])
    one_agent(agent)
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    await _collect(resp)
    assert agent.chain == ("faker",)


class _RunDeliveryRecordingAgent(_FakeAgent):
    """Records the ambient run-delivery id seen inside its drive, so the door's minting frame is observable."""

    def __init__(self, events: list[Any] | None = None) -> None:
        super().__init__(events)
        self.run_delivery_id: str | None = "unset"

    async def astream(self, **kwargs: Any):  # type: ignore[override]
        self.run_delivery_id = get_run_delivery_id()
        async for event in super().astream(**kwargs):
            yield event


async def test_run_drives_under_a_run_delivery_context(one_agent):
    # The SSE door's minting frame binds a run-delivery context whose id an async park captures —
    # the seam that makes a caller ask park-capable even though the live SSE receiver takes the
    # outcome inline and no out-of-band address is bound.
    agent = _RunDeliveryRecordingAgent([MessageFinal(text="ok")])
    one_agent(agent)
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    await _collect(resp)
    assert agent.run_delivery_id != "unset"
    assert agent.run_delivery_id is not None


async def test_run_finish_emits_the_final_frame_result(one_agent):
    # ``result``: a structured final is captured by the drive and re-emitted as a ``structured_final`` frame.
    one_agent(_FakeAgent([StructuredFinal(data={"answer": 7})]))
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    frames = _data_frames(await _collect(resp))
    assert [f["type"] for f in frames] == ["structured_final", "stream.end"]
    assert StructuredFinal.model_validate(frames[0]).data == {"answer": 7}


async def test_run_user_park_emits_the_suspended_frame_parked(one_agent, monkeypatch):
    # ``parked``: an async ask to the USER surfaces as the suspended frame carrying the park's
    # interaction ids (today's ``SuspendedFinal`` shape). With no caller ask in the run's parked
    # list, the whole park classifies as ``parked``.
    monkeypatch.delenv("INTERACTIONS_REDIS_URL", raising=False)
    one_agent(_FakeAgent([SuspendedFinal(interaction_ids=["i1", "i2"], thread_id="t1")]))
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    frames = _data_frames(await _collect(resp))
    assert [f["type"] for f in frames] == ["suspended_final", "stream.end"]
    assert SuspendedFinal.model_validate(frames[0]).interaction_ids == ["i1", "i2"]


async def test_run_caller_ask_emits_the_asks_frame(one_agent, monkeypatch):
    # ``asks``: an async ask to the CALLER (the live SSE client) surfaces as the ask-entries frame,
    # NOT the suspended frame. ``visit`` classifies a caller ask (its store-backed caller/user split
    # is covered by the visit unit tests); the door maps a ``kind="asks"`` outcome to ``asks_final``.
    from tai42_contract.interactions import VisitOutcome

    entry = ParkedEntry(id="i1", status="asking", to="caller", question="pick?", answer_format="text")

    async def _fake_visit(*, start, **_kwargs) -> VisitOutcome:
        await start({})
        return VisitOutcome(action="started", cancelled=[], kind="asks", asks=[entry])

    monkeypatch.setattr("tai42_skeleton.interactions.visit.visit", _fake_visit)
    one_agent(_FakeAgent([SuspendedFinal(interaction_ids=["i1"], thread_id="t1")]))
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    frames = _data_frames(await _collect(resp))
    assert [f["type"] for f in frames] == ["asks_final", "stream.end"]
    asks = AsksFinal.model_validate(frames[0]).asks
    assert [a["id"] for a in asks] == ["i1"]


async def test_run_interrupt_streams_and_ends_with_no_extra_terminal_frame(one_agent):
    # ``none``: an interrupt is NOT a park — it streams as ``interrupt_final`` and the drive ends
    # there, with no extra terminal frame before ``stream.end``.
    one_agent(_FakeAgent([InterruptFinal(interrupt_id="x1", payload={"opts": [1, 2]}, reason="choose")]))
    resp = await router.run_agent(_make_run_request("faker", b'{"prompt":"hi"}'))
    frames = _data_frames(await _collect(resp))
    assert [f["type"] for f in frames] == ["interrupt_final", "stream.end"]
    assert InterruptFinal.model_validate(frames[0]).interrupt_id == "x1"


def test_agent_run_request_models_carry_no_extras_field() -> None:
    # ``extras`` is an in-process seam keyword only: no request field on the SSE doors can set it,
    # and the direct run-tool / submit request models expose only tool_name/arguments/subject.
    from tai42_skeleton.operations.tool_runs.models import ToolRunSubmission
    from tai42_skeleton.operations.tools import RunToolRequest

    assert "extras" not in RunToolRequest.model_fields
    assert "extras" not in ToolRunSubmission.model_fields
    assert set(RunToolRequest.model_fields) == {"tool_name", "arguments", "subject"}
    assert set(ToolRunSubmission.model_fields) == {"tool_name", "arguments", "subject"}


async def test_run_subject_query_deposits_the_api_state_context(one_agent, monkeypatch):
    # The SSE door reads its subject off the query and deposits the ``door="api"`` state context, so
    # the drive runs under the caller's named subject (an async park would index there).
    from tai42_skeleton.states.context import current_state_context

    seen: dict[str, Any] = {}

    class _SubjectRecordingAgent(_FakeAgent):
        async def astream(self, **kwargs: Any):  # type: ignore[override]
            ctx = current_state_context()
            seen["door"] = ctx.door if ctx is not None else None
            seen["by_kind"] = dict(ctx.candidates.by_kind) if ctx is not None else None
            async for event in super().astream(**kwargs):
                yield event

    one_agent(_SubjectRecordingAgent([MessageFinal(text="ok")]))
    request = _make_run_request("faker", b'{"prompt":"hi"}')
    request.scope["query_string"] = b"subject_kind=thread&subject_key=t-1&subject_target=faker"
    resp = await router.run_agent(request)
    await _collect(resp)
    assert seen["door"] == "api"
    assert seen["by_kind"] == {"thread": "t-1"}


async def test_run_subject_query_partial_is_a_400(one_agent):
    one_agent(_FakeAgent([MessageFinal(text="ok")]))
    request = _make_run_request("faker", b'{"prompt":"hi"}')
    request.scope["query_string"] = b"subject_kind=thread"
    resp = await router.run_agent(request)
    assert resp.status_code == 400
