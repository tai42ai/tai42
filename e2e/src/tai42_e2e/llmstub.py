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
    body — so they never consume a chat turn.

    It also serves ``/v1/systemone`` with DETERMINISTIC classification answers
    (each answer hashed from the request's ``state`` and the question), so the
    classify tool runs offline and a ``state`` dropped or corrupted on the wire
    shows up as a different answer. The classifier vendor points at
    :attr:`root_url` (no ``/v1``) and appends ``/v1/systemone`` itself. These
    calls are NOT scripted either."""

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
        # When set, a completion carrying a FORCED ``tool_choice`` is answered with the
        # vendor's 400 instead of a scripted turn — the refusing-model leg that proves the
        # native structured-output plan sends no forced choice.
        self._refuse_forced_tool_choice = False
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

    @property
    def root_url(self) -> str:
        """The stub's site root (no ``/v1`` path); the classifier vendor appends
        ``/v1/systemone`` beneath it itself."""
        return f"http://{self.host}:{self.port}"

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
        self._refuse_forced_tool_choice = False

    def refuse_forced_tool_choice(self) -> None:
        """Answer the vendor's 400 to any completion that binds a forced ``tool_choice``.

        A structured-output run that still succeeds with this on proves no forced choice
        reached the wire (the native plan), reproducing a refusing model's behaviour.
        """
        self._refuse_forced_tool_choice = True

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
            raise _UnscriptedError()
        return self._queue.popleft()

    async def _serve_completion(self, body: dict[str, Any]) -> Any:
        """Serve one scripted chat-completion turn (streaming or whole), tracking
        in-flight concurrency and refusing loudly when the script queue is empty."""
        self._in_flight_completions += 1
        self._max_in_flight_completions = max(self._max_in_flight_completions, self._in_flight_completions)
        try:
            self._requests.append(body)
            choice = body.get("tool_choice")
            if self._refuse_forced_tool_choice and choice not in (None, "auto", "none"):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {"message": 'tool_choice: type "tool" and "any" are not supported for this model.'}
                    },
                )
            try:
                turn = self._next_turn()
            except _UnscriptedError:
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

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def chat_completions(request: Request) -> Any:
            return await self._serve_completion(await request.json())

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

        @app.post("/v1/systemone")
        async def systemone(request: Request) -> Any:
            """Answer a TypeSafe classification request DETERMINISTICALLY, so the
            classify tool runs entirely offline. The request body carries a
            ``model``, the ``state`` under judgement, and a non-empty
            ``questions`` map; each answer is hashed from the ``state``, the
            question's name, and the question body, so an identical request always
            yields the identical answer and any change to the ``state`` on the wire
            yields a different answer. The response is a vendor
            ``ClassifierResponse`` body, and the ``x-typesafe-request-id`` header
            is what the vendor copies into ``ClassifierResponse.request_id``.

            A body with no non-empty ``questions`` map, or a question with an
            unrecognized ``type`` or missing criteria, is a malformed call refused
            loudly (a 400), never served a plausible-looking answer."""
            body = await request.json()
            questions = body.get("questions")
            if not isinstance(questions, dict) or not questions:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": f"llmstub: /v1/systemone requires a non-empty 'questions' map; got {body!r}"
                        }
                    },
                )
            state = body.get("state")
            try:
                answers = {name: _classifier_answer(name, question, state) for name, question in questions.items()}
            except _MalformedQuestionError as error:
                return JSONResponse(status_code=400, content={"error": {"message": f"llmstub: {error}"}})
            return JSONResponse(
                content={
                    "model": body.get("model", "e2e-classify"),
                    "answers": answers,
                    "usage": {"input_tokens": len(questions), "output_tokens": len(answers)},
                },
                headers={"x-typesafe-request-id": _classifier_request_id(body)},
            )

        self._install_control_routes(app)
        return app

    def _install_control_routes(self, app: FastAPI) -> None:
        """The out-of-process control plane: a standalone runner (the browser-e2e
        studio stack) boots this stub, and a Playwright spec in a separate Node
        process scripts it and reads its request log over these routes rather than
        through the in-process ``script``/``requests`` API the pytest suite uses."""

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


class _UnscriptedError(Exception):
    """Raised when a completion arrives with an empty script queue."""


class _MalformedQuestionError(Exception):
    """Raised when a ``/v1/systemone`` question lacks a recognized ``type`` or the
    criteria that type requires."""


def _classifier_answer(name: str, question: Any, state: Any) -> dict[str, Any]:
    """A deterministic vendor answer for one classification question, hashed from
    the request's ``state``, the question's ``name``, and the question body, so an
    identical request reproduces the answer and any change to the ``state`` moves
    it. Every answer carries the ``type`` discriminator the vendor's answer models
    require, and every distribution is a valid vendor shape (probabilities in
    ``[0, 1]`` summing to 1).

    A ``noul`` answer is one hashed fraction. A ``choice`` answer hashes one weight
    per label (the label appended to the seed), normalises them to a distribution,
    and picks the argmax label with the peak probability as its confidence. A
    ``score`` answer hashes one weight per ordinal level, normalises them to a
    distribution keyed by integer level, and reports the expected level as its
    score with the peak probability as its confidence."""
    if not isinstance(question, dict):
        raise _MalformedQuestionError(f"a question must be an object; got {question!r}")
    question_type = question.get("type")
    seed = _answer_seed(state, name, question)
    if question_type == "noul":
        return {"type": "noul", "noul": _hash_fraction(seed)}
    if question_type == "choice":
        criteria = question.get("criteria")
        if not isinstance(criteria, dict) or not criteria:
            raise _MalformedQuestionError(f"a 'choice' question needs a non-empty 'criteria' map; got {question!r}")
        labels = list(criteria)
        probabilities = _normalise([_hash_fraction(f"{seed}{label}") for label in labels])
        distribution = dict(zip(labels, probabilities, strict=True))
        return {
            "type": "choice",
            "choice": max(distribution, key=distribution.__getitem__),
            "probabilities": distribution,
            "confidence": max(probabilities),
        }
    if question_type == "score":
        criteria = question.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise _MalformedQuestionError(f"a 'score' question needs a non-empty 'criteria' list; got {question!r}")
        levels = range(len(criteria))
        probabilities = _normalise([_hash_fraction(f"{seed}{level}") for level in levels])
        return {
            "type": "score",
            "score": sum(level * probabilities[level] for level in levels),
            "legend": {level: criteria[level] for level in levels},
            "probabilities": {level: probabilities[level] for level in levels},
            "confidence": max(probabilities),
        }
    raise _MalformedQuestionError(f"a question 'type' must be 'noul', 'choice', or 'score'; got {question_type!r}")


def _answer_seed(state: Any, name: str, question: Any) -> str:
    """The hash seed for one question's answer: the request ``state``, the
    question name, and the question body, so the answer moves with any of them."""
    return json.dumps(state, sort_keys=True, default=str) + name + json.dumps(question, sort_keys=True, default=str)


def _hash_fraction(text: str) -> float:
    """A deterministic float in ``[0, 1]``: the first 8 hex digits of
    ``sha256(text)`` over ``0xFFFFFFFF``."""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF


def _normalise(weights: list[float]) -> list[float]:
    """Scale non-negative hashed weights into a probability distribution summing to
    1. A zero total (every hashed weight zero) has no distribution and raises."""
    total = sum(weights)
    if total <= 0:
        raise _MalformedQuestionError(f"hashed weights sum to {total}; no probability distribution exists")
    return [weight / total for weight in weights]


def _classifier_request_id(body: dict[str, Any]) -> str:
    """A stable id per classification body; the vendor copies the
    ``x-typesafe-request-id`` response header into ``ClassifierResponse.request_id``."""
    seed = json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    return f"e2e-{hashlib.sha256(seed).hexdigest()[:16]}"


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
