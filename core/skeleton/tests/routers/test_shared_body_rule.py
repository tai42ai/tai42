"""Every door that reads its own JSON body answers the one shared set of ``400`` texts.

``invalid JSON body`` for a body that does not parse, ``body must be a JSON object`` for one that is
not an object, and ``invalid <what>: <rule>; <rule>`` naming each violated rule, never the value.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import Response

from tai42_skeleton.operations.errors import BadRequestError
from tai42_skeleton.routers import agents, backend, backup, setup, storage
from tai42_skeleton.routers.conversations import extractors

_PATH_PARAMS = {"route_name": "synthetic-line", "target_kind": "tool", "target_name": "synthetic"}


def _request(body: bytes) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/synthetic",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "path_params": dict(_PATH_PARAMS),
        "client": ("127.0.0.1", 50000),
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


_EXTRACTORS = [
    storage._extract_upload,
    backend._reload_targets,
    setup._extract_setup,
    backup._extract_import,
    extractors._extract_route_create,
    extractors._extract_person_locale_body,
    extractors._extract_thread_message,
    extractors._extract_thread_mode_body,
    extractors._extract_target_config,
]

_REFUSALS = [(b"{not json", "invalid JSON body"), (b"[1, 2]", "body must be a JSON object")]


@pytest.mark.parametrize("extract", _EXTRACTORS, ids=lambda fn: f"{fn.__module__}.{fn.__name__}")
@pytest.mark.parametrize(("raw", "message"), _REFUSALS)
async def test_an_operation_door_refuses_with_the_shared_text(extract, raw: bytes, message: str) -> None:
    with pytest.raises(BadRequestError) as excinfo:
        await extract(_request(raw))
    assert excinfo.value.status == 400
    assert excinfo.value.message == message


def _error_of(response: Response) -> str:
    assert response.status_code == 400
    return json.loads(bytes(response.body))["error"]


@pytest.mark.parametrize(("raw", "message"), _REFUSALS)
async def test_the_agent_run_door_refuses_with_the_shared_text(raw: bytes, message: str) -> None:
    result = await agents._build_run_kwargs(_request(raw), SimpleNamespace())  # type: ignore[arg-type]
    assert isinstance(result, Response)
    assert _error_of(result) == message


@pytest.mark.parametrize(("raw", "message"), _REFUSALS)
async def test_the_backup_export_door_refuses_with_the_shared_text(raw: bytes, message: str) -> None:
    assert _error_of(await backup.export_backup(_request(raw))) == message


@pytest.mark.parametrize(
    ("extract", "what", "fields"),
    [
        (extractors._extract_route_create, "conversation route", {"door": 12345678, "x": "s3cr3t-v4lue"}),
        (
            extractors._extract_target_config,
            "conversation config",
            {"multichannel": "s3cr3t-v4lue", "greeting_template": 12345678},
        ),
    ],
)
async def test_a_model_refusal_names_the_rule_and_never_the_value(extract, what: str, fields: dict) -> None:
    body = json.dumps(fields).encode()
    with pytest.raises(BadRequestError) as excinfo:
        await extract(_request(body))
    message = excinfo.value.message
    assert message.startswith(f"invalid {what}: ")
    assert "s3cr3t-v4lue" not in message
    assert "12345678" not in message


# -- the doors that read their bounded body bytes ------------------------------------


@pytest.mark.parametrize(
    ("extract", "raw", "message"),
    [
        ("_extract_answer", b"{not json", "invalid JSON body"),
        ("_extract_answer", b"[1, 2]", "body must be a JSON object"),
        ("_extract_answer", b"{}", "body must contain 'answer'"),
        ("_extract_react", b"[1, 2]", "body must be a JSON object"),
        ("_extract_react", b'{"event": {}}', "body must contain 'event' and 'values'"),
    ],
)
async def test_the_in_app_answer_and_react_doors_answer_the_shared_texts(
    extract: str, raw: bytes, message: str
) -> None:
    # The package re-exports the door handlers under the module names; read the module itself.
    answer = importlib.import_module("tai42_skeleton.routers.interactions.answer")

    with pytest.raises(BadRequestError) as excinfo:
        await getattr(answer, extract)(_request(raw))
    assert excinfo.value.message == message


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{not json", "invalid JSON body"),
        (b"[1, 2]", "body must be a JSON object"),
        (b'{"event": {}}', "body must contain 'event' and 'values'"),
    ],
)
def test_the_callback_react_body_answers_the_shared_texts(raw: bytes, message: str) -> None:
    from tai42_skeleton.routers.interactions.callback_react import _parse_react_body

    result = _parse_react_body(raw)
    assert isinstance(result, Response)
    assert _error_of(result) == message


@pytest.mark.parametrize(
    ("raw", "message"), [(b"{not json", "invalid JSON body"), (b"[1]", "body must be a JSON object")]
)
def test_the_callback_typed_answer_body_answers_the_shared_texts(raw: bytes, message: str) -> None:
    from tai42_contract.interactions import AnswerFormat

    from tai42_skeleton.routers.interactions.callback import _parse_typed_answer

    result = _parse_typed_answer(raw, AnswerFormat.TEXT)
    assert isinstance(result, Response)
    assert _error_of(result) == message


def test_the_tool_call_subject_refusal_names_the_rule_and_never_the_value() -> None:
    from tai42_skeleton.routers._tool_call import ToolCallRequestError, _read_subject

    with pytest.raises(ToolCallRequestError) as excinfo:
        _read_subject({"target_kind": "bogus-kind-value", "target_name": "n", "kind": "thread", "key": "k"})
    message = str(excinfo.value)
    assert message.startswith("invalid 'subject': ")
    assert "bogus-kind-value" not in message
    assert "pydantic.dev" not in message


async def test_a_deeply_nested_body_is_the_invalid_json_400() -> None:
    from tai42_skeleton.operations.adapter import read_json_object

    nested = b"[" * 100_000 + b"]" * 100_000
    with pytest.raises(BadRequestError) as excinfo:
        await read_json_object(_request(nested))
    assert excinfo.value.message == "invalid JSON body"


@pytest.mark.parametrize("subject", ["x", [], 7])
def test_a_subject_that_is_not_an_object_is_refused_by_the_model_rule(subject: object) -> None:
    from tai42_skeleton.routers._tool_call import ToolCallRequestError, _read_subject

    with pytest.raises(ToolCallRequestError) as excinfo:
        _read_subject(subject)
    assert excinfo.value.status_code == 400
    assert str(excinfo.value).startswith("invalid 'subject': Input should be a valid dictionary")
