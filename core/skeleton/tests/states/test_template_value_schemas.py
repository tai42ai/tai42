"""A template's parameter and declarations schemas: checked when the template is written, never fetched.

Each parameter value and each attachment's declarations are validated against a schema the
template author writes. Every template write door (the save, the render preview, the seed
applier) refuses such a schema that breaks the record schema's document rules (one whose ``$ref``
does not resolve inside it among them), and the attach validators resolve references with no
schema fetched. Driven against the in-memory ``FakeStatesStore`` and a local HTTP server that
counts every request.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
import referencing.exceptions
from tai42_contract.app import tai42_app
from tai42_contract.states.errors import TemplateValidationError
from tai42_contract.states.models import AttachBody, StateTemplateDeclarations, StateTemplateParameter

from tai42_skeleton.states import service as service_mod
from tai42_skeleton.states.service import StatesService
from tai42_skeleton.states.service.attachments import _validate_attach_declarations, _validate_effective_parameters
from tai42_skeleton.states.templates.validate import validate_template

from .fake_service_store import _STATE, FakeStatesStore, _FakeApp, _template_doc

_FRAGMENT: dict[str, Any] = {"type": "object", "properties": {"count": {"$parameter": "cap"}}}


@pytest.fixture
def hits() -> Iterator[tuple[str, list[str]]]:
    """A local schema server: its base URL and the paths it was asked for."""
    asked: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            asked.append(self.path)
            body = json.dumps({"type": "integer"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", asked
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> Iterator[StatesService]:
    monkeypatch.setattr(service_mod, "states_store_configured", lambda: True)
    with tai42_app.bound(_FakeApp()):
        yield StatesService(store=FakeStatesStore())  # type: ignore[arg-type]


def _parameter_doc(schema: dict[str, Any]) -> Any:
    return _template_doc("p", parameters={"cap": {"schema": schema}}, schema=_FRAGMENT)


def _declarations_doc(schema: dict[str, Any]) -> Any:
    return _template_doc("d", declarations={"schema": schema})


_REFUSED: list[tuple[str, dict[str, Any], str]] = [
    (
        "parameter",
        {"$ref": "{url}/param.json"},
        "parameter 'cap' schema breaks the JSON Schema rules at $ref: $ref '{url}/param.json' does not resolve: "
        "it names a schema outside this one, and no schema is fetched",
    ),
    (
        "declarations",
        {"$ref": "{url}/decl.json"},
        "declarations schema breaks the JSON Schema rules at $ref: $ref '{url}/decl.json' does not resolve: "
        "it names a schema outside this one, and no schema is fetched",
    ),
    (
        "parameter",
        {"type": "object", "properties": {"n": {"$ref": "#/$defs/missing"}}},
        "parameter 'cap' schema property 'n' breaks the JSON Schema rules: $ref '#/$defs/missing' does not resolve",
    ),
    ("parameter", {"type": 5}, "parameter 'cap' schema breaks the JSON Schema rules at type: 5 is not valid"),
    ("declarations", {"minLength": -1}, "declarations schema breaks the JSON Schema rules at minLength: -1 is less"),
    (
        "parameter",
        {
            "type": "object",
            "properties": {
                "a": {"type": "object", "patternProperties": {"x": {}, "(?i)y": {}}, "additionalProperties": False}
            },
        },
        "parameter 'cap' schema property 'a' breaks the JSON Schema rules: the keys joined with '|'",
    ),
    (
        "declarations",
        {"type": "object", "properties": {"a": {"type": "number", "maximum": float("nan")}}},
        "declarations schema property 'a' has a value at maximum that is not a finite number (nan)",
    ),
    (
        "parameter",
        {"$id": "https://e.com/s", "$ref": "https://e.com/s#/x", "x": {"$dynamicRef": "#/$defs/d"}, "$defs": {"d": {}}},
        "parameter 'cap' schema: $dynamicRef is not supported",
    ),
    (
        "declarations",
        {"$id": "https://e.com/s", "$ref": "s#/x", "x": {"$schema": "http://json-schema.org/draft-07/schema#"}},
        "declarations schema: $schema 'http://json-schema.org/draft-07/schema#' is not supported",
    ),
]


def _doc(kind: str, schema: dict[str, Any]) -> Any:
    return _parameter_doc(schema) if kind == "parameter" else _declarations_doc(schema)


def _filled(value: Any, url: str) -> Any:
    return json.loads(json.dumps(value).replace("{url}", url))


@pytest.mark.parametrize(("kind", "schema", "message"), _REFUSED)
@pytest.mark.parametrize("door", ["put", "render", "seed"])
async def test_every_template_write_door_refuses_a_value_schema_it_cannot_check(
    svc: StatesService, hits: tuple[str, list[str]], door: str, kind: str, schema: dict[str, Any], message: str
) -> None:
    url, asked = hits
    doc = _doc(kind, _filled(schema, url))
    with pytest.raises(TemplateValidationError, match=re.escape(_filled(message, url))):
        await _write(svc, door, doc)
    assert asked == []


async def _write(svc: StatesService, door: str, doc: Any) -> None:
    # One template write door: the save, the render preview, or the seed applier.
    if door == "put":
        await svc.put_template(doc, replace=False)
    elif door == "render":
        await svc.render_template(doc)
    else:
        svc.register_template_seed(doc)
        await svc.apply_template_seeds()


async def test_attach_checks_values_against_value_schemas_with_local_references(svc: StatesService) -> None:
    local = {"$defs": {"n": {"type": "integer"}}, "$ref": "#/$defs/n"}
    doc = _template_doc(
        "p",
        parameters={"cap": {"schema": {"type": "object"}}, "size": {"schema": local, "default": 1}},
        schema=_FRAGMENT,
        declarations={
            "schema": {
                "$defs": {"n": {"type": "integer"}},
                "type": "object",
                "properties": {"n": {"$ref": "#/$defs/n"}},
            }
        },
    )
    await svc.put_declaration(_STATE)
    await svc.put_template(doc, replace=False)
    with pytest.raises(TemplateValidationError, match="attach parameter 'size' is invalid: 'x' is not of type"):
        await svc.attach("alerts", "p", AttachBody(path=["sub"], parameters={"cap": {}, "size": "x"}))
    with pytest.raises(TemplateValidationError, match="attach declarations are invalid"):
        await svc.attach("alerts", "p", AttachBody(path=["sub"], parameters={"cap": {}}, declarations={"n": "x"}))
    await svc.attach("alerts", "p", AttachBody(path=["sub"], parameters={"cap": {}, "size": 2}, declarations={"n": 3}))


def _unchecked_template(*, parameter: dict[str, Any] | None = None, declarations: dict[str, Any] | None = None) -> Any:
    # A template as the attach validators receive it, holding value schemas no write door saw:
    # the validators themselves must fetch nothing.
    template = validate_template(_parameter_doc({}).model_dump(by_alias=True))
    return dataclasses.replace(
        template,
        parameters={"cap": StateTemplateParameter.model_validate({"schema": parameter or {}})},
        declarations=None
        if declarations is None
        else StateTemplateDeclarations.model_validate({"schema": declarations}),
    )


def test_the_attach_parameter_validator_fetches_no_schema(hits: tuple[str, list[str]]) -> None:
    url, asked = hits
    template = _unchecked_template(parameter={"$ref": f"{url}/param.json"})
    with pytest.raises(referencing.exceptions.Unresolvable):
        _validate_effective_parameters(template, {"cap": 1})
    assert asked == []


async def test_the_attach_declarations_validator_fetches_no_schema(hits: tuple[str, list[str]]) -> None:
    url, asked = hits
    template = _unchecked_template(declarations={"$ref": f"{url}/decl.json"})
    with pytest.raises(referencing.exceptions.Unresolvable):
        await _validate_attach_declarations(template, {"n": 1}, {})
    assert asked == []
