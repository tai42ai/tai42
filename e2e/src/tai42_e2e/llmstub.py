"""A scripted, deterministic OpenAI-compatible chat-completions server.

Monkeypatching the LLM cannot cross a process boundary, so the seam moves to
where production configures it: the SUT builds ``ChatOpenAI`` from
``LLM_BASE_URL`` / ``LLM_API_KEY`` / ``LLM_MODEL``, which an agent stack points
at this server. Turns are enqueued ahead of time via ``POST /_script``; an
empty queue on arrival is a loud 500, never a silent default reply, so an
unscripted call fails the test."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections import deque
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.ports import allocate_port


class LlmStub:
    """A threaded scripted LLM. Script a list of turns, each either
    ``{"content": str}`` (a final assistant message) or
    ``{"tool_call": {"name", "arguments"}}`` (a single tool call). Reset the
    queue between agent tests.

    It also serves ``/v1/embeddings`` with DETERMINISTIC vectors (a fixed-length
    hash of each input item), so the vector-store agents (retrieval) embed and
    search entirely offline. Embedding calls are NOT scripted — they answer any
    body — so they never consume a chat turn."""

    # The fixed embedding dimensionality the stub reports; small but nonzero so a
    # store index has a stable width across put + query + the dims probe.
    _EMBEDDING_DIMS = 16

    def __init__(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.host = host
        # A caller may pin the port (the browser-e2e runner does, so a Playwright
        # spec can script the stub over HTTP at a known origin); otherwise take an
        # ephemeral one.
        self.port = port if port is not None else allocate_port()
        self._queue: deque[dict[str, Any]] = deque()
        self._requests: list[dict[str, Any]] = []
        # Per-completion delay and in-flight high-water mark for observing turn concurrency.
        # Mutated only on the stub's single-threaded serving loop, so no lock is needed.
        self._response_delay_seconds = 0.0
        self._in_flight_completions = 0
        self._max_in_flight_completions = 0
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def base_url(self) -> str:
        """The ``LLM_BASE_URL`` an agent stack points at (OpenAI ``/v1`` root)."""
        return f"http://{self.host}:{self.port}/v1"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def script(self, turns: list[dict[str, Any]]) -> None:
        """Replace the pending turn queue."""
        self._queue = deque(turns)

    def reset(self) -> None:
        self._queue.clear()
        self._requests.clear()
        self._response_delay_seconds = 0.0
        self._in_flight_completions = 0
        self._max_in_flight_completions = 0

    @property
    def requests(self) -> list[dict[str, Any]]:
        """The recorded request bodies, for asserting what the agent sent."""
        return list(self._requests)

    def set_response_delay(self, seconds: float) -> None:
        """Hold every chat completion open for ``seconds`` before answering, so concurrent
        turns overlap long enough for :attr:`max_in_flight_completions` to see the peak."""
        self._response_delay_seconds = seconds

    @property
    def max_in_flight_completions(self) -> int:
        """The most chat completions ever in flight at once since the last reset — the peak
        concurrent turns, as the turn engine's global ceiling bounds it."""
        return self._max_in_flight_completions

    def _next_turn(self) -> dict[str, Any]:
        if not self._queue:
            raise _Unscripted()
        return self._queue.popleft()

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def chat_completions(request: Request) -> Any:
            body = await request.json()
            self._in_flight_completions += 1
            self._max_in_flight_completions = max(self._max_in_flight_completions, self._in_flight_completions)
            try:
                self._requests.append(body)
                try:
                    turn = self._next_turn()
                except _Unscripted:
                    return JSONResponse(
                        status_code=500,
                        content={"error": {"message": "llmstub: no scripted turn for this call"}},
                    )
                if self._response_delay_seconds > 0:
                    # Server-side response latency, not a client poll — holds the completion
                    # open so concurrent turns overlap.
                    await asyncio.sleep(self._response_delay_seconds)  # noqa: TID251
                if body.get("stream"):
                    return StreamingResponse(_stream(turn, body), media_type="text/event-stream")
                return JSONResponse(content=_completion(turn, body))
            finally:
                self._in_flight_completions -= 1

        @app.post("/v1/embeddings")
        async def embeddings(request: Request) -> Any:
            """Return a deterministic vector per input item. OpenAI-compatible:
            the request ``input`` is a string or a list of strings/token-id arrays,
            and the response carries one ``embedding`` per item, in order. Identical
            input always yields the identical vector, so a store round-trip is
            reproducible offline.

            A request with no ``input`` is a malformed call, not a request for a
            default: it is refused loudly (like an unscripted completion) so a
            client that stops sending the field fails its test instead of being
            served a plausible-looking vector."""
            body = await request.json()
            raw = body.get("input")
            if raw is None or (isinstance(raw, list) and not raw):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {"message": f"llmstub: /v1/embeddings requires a non-empty 'input'; got {body!r}"}
                    },
                )
            items = raw if isinstance(raw, list) else [raw]
            data = [
                {"object": "embedding", "index": i, "embedding": _deterministic_vector(item)}
                for i, item in enumerate(items)
            ]
            return JSONResponse(
                content={
                    "object": "list",
                    "data": data,
                    "model": body.get("model", "e2e-embed"),
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                }
            )

        # Out-of-process control plane: a standalone runner (the browser-e2e
        # studio stack) boots this stub, and a Playwright spec in a separate Node
        # process scripts it and reads its request log over these routes rather
        # than through the in-process `script`/`requests` API the pytest suite
        # uses.
        @app.post("/_script")
        async def script_turns(request: Request) -> Any:
            body = await request.json()
            turns = body.get("turns")
            if not isinstance(turns, list):
                return JSONResponse(status_code=400, content={"error": {"message": 'body must be {"turns": [...]}'}})
            self.script(turns)
            return JSONResponse(content={"scripted": len(turns)})

        @app.post("/_reset")
        async def reset_state() -> Any:
            self.reset()
            return JSONResponse(content={"reset": True})

        @app.get("/_requests")
        async def recorded_requests() -> Any:
            return JSONResponse(content={"count": len(self._requests), "requests": self.requests})

        return app


class _Unscripted(Exception):
    """Raised when a completion arrives with an empty script queue."""


def _deterministic_vector(item: Any) -> list[float]:
    """A stable unit-norm float vector for one embedding input item.

    The item (a string, or a token-id list when the client tokenizes) is
    JSON-serialized and hashed; the digest bytes seed the vector components. The
    same item always maps to the same vector, so embed-then-search is
    reproducible without any model."""
    seed = json.dumps(item, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    dims = LlmStub._EMBEDDING_DIMS
    # Stretch the 32-byte digest to the required width deterministically.
    raw = (digest * (dims // len(digest) + 1))[:dims]
    vector = [(byte / 255.0) * 2.0 - 1.0 for byte in raw]
    norm = math.sqrt(sum(component * component for component in vector)) or 1.0
    return [component / norm for component in vector]


def _message(turn: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Build the assistant message + finish_reason for a scripted turn."""
    if "tool_call" in turn:
        call = turn["tool_call"]
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments", {}))},
                }
            ],
        }
        return message, "tool_calls"
    if "content" not in turn:
        raise ValueError(f"llmstub turn has neither 'content' nor 'tool_call': {turn!r}")
    return {"role": "assistant", "content": turn["content"]}, "stop"


def _completion(turn: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    message, finish_reason = _message(turn)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "e2e-scripted"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _stream(turn: dict[str, Any], body: dict[str, Any]):
    """Frame a scripted turn as OpenAI streaming SSE chunks."""
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    model = body.get("model", "e2e-scripted")
    message, finish_reason = _message(turn)

    def chunk(delta: dict[str, Any], finish: str | None) -> str:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    yield chunk({"role": "assistant"}, None)
    if finish_reason == "tool_calls":
        call = message["tool_calls"][0]
        yield chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["function"]["name"], "arguments": call["function"]["arguments"]},
                    }
                ]
            },
            None,
        )
    else:
        yield chunk({"content": message["content"]}, None)
    yield chunk({}, finish_reason)
    yield "data: [DONE]\n\n"
