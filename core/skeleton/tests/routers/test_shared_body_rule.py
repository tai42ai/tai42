"""Every door that reads its own JSON body answers the one shared set of ``400`` texts.

``invalid JSON body`` for a body that does not parse, ``body must be a JSON object`` for one that is
not an object, and ``invalid <what>: <rule>; <rule>`` naming each violated rule, never the value.
"""

from __future__ import annotations

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
    ("extract", "what"),
    [
        (extractors._extract_route_create, "conversation route"),
        (extractors._extract_target_config, "conversation config"),
    ],
)
async def test_a_model_refusal_names_the_rule_and_never_the_value(extract, what: str) -> None:
    body = json.dumps({"door": 12345678, "secret_field_value": "s3cr3t-v4lue"}).encode()
    with pytest.raises(BadRequestError) as excinfo:
        await extract(_request(body))
    message = excinfo.value.message
    assert message.startswith(f"invalid {what}: ")
    assert "s3cr3t-v4lue" not in message
    assert "12345678" not in message
