"""The shared HTTP-edge body helpers: a JSON-object parser, a value-free validator, a request over bounded bytes."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field
from starlette.requests import Request

from tai42_skeleton.operations.adapter import read_json_object, request_with_body, validate_body
from tai42_skeleton.operations.errors import BadRequestError


def _request(body: bytes, content_type: str = "application/json") -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            raise AssertionError("the original stream is read once")
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/synthetic",
        "query_string": b"",
        "headers": [(b"content-type", content_type.encode())],
    }
    return Request(scope, receive)


# -- read_json_object ----------------------------------------------------------


async def test_read_json_object_returns_the_object() -> None:
    assert await read_json_object(_request(b'{"a": 1}')) == {"a": 1}


@pytest.mark.parametrize(
    ("raw", "message"),
    [(b"{not json", "invalid JSON body"), (b"[1, 2]", "body must be a JSON object"), (b"", "invalid JSON body")],
)
async def test_read_json_object_refuses_with_a_400_that_echoes_nothing(raw: bytes, message: str) -> None:
    with pytest.raises(BadRequestError) as excinfo:
        await read_json_object(_request(raw))
    assert excinfo.value.status == 400
    assert excinfo.value.message == message


# -- validate_body ---------------------------------------------------------------


class _Probe(BaseModel):
    name: str = Field(min_length=3)
    count: int


def test_validate_body_returns_the_model() -> None:
    assert validate_body(_Probe, {"name": "abc", "count": 2}, "probe params") == _Probe(name="abc", count=2)


def test_validate_body_names_each_rule_and_never_the_value() -> None:
    with pytest.raises(BadRequestError) as excinfo:
        validate_body(_Probe, {"name": "s3", "count": "secret-value"}, "probe params")
    message = excinfo.value.message
    assert message.startswith("invalid probe params: ")
    assert "at least 3 characters" in message
    assert "valid integer" in message
    assert "secret-value" not in message
    assert "s3" not in message


# -- request_with_body -------------------------------------------------------------


async def test_the_built_request_reads_the_given_bytes_as_body_and_json() -> None:
    original = _request(b"ignored")
    raw = json.dumps({"k": "v"}).encode()
    built = request_with_body(original, raw)
    assert await built.body() == raw
    # A second read replays the same bytes.
    assert await built.body() == raw
    assert await built.json() == {"k": "v"}
    assert built.scope is original.scope


async def test_the_built_request_parses_an_urlencoded_form() -> None:
    original = _request(b"", "application/x-www-form-urlencoded")
    form = await request_with_body(original, b"a=1&b=two").form()
    assert dict(form) == {"a": "1", "b": "two"}


async def test_the_built_request_parses_a_multipart_form() -> None:
    boundary = "synthetic-boundary"
    raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="field"\r\n\r\nvalue\r\n--{boundary}--\r\n').encode()
    original = _request(b"", f"multipart/form-data; boundary={boundary}")
    form = await request_with_body(original, raw).form()
    assert form["field"] == "value"


async def test_the_built_stream_ends_in_a_disconnect() -> None:
    built = request_with_body(_request(b""), b"x")
    first = await built.receive()
    assert first == {"type": "http.request", "body": b"x", "more_body": False}
    assert await built.receive() == {"type": "http.disconnect"}
