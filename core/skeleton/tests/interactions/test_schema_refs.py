"""A schema's ``$ref``s are resolved when the question is asked, and none is ever fetched.

The answer check follows every ``$ref``/``$dynamicRef`` it meets; one it cannot follow, one
pointing at a value that is no schema, a target breaking the rules and a cycle of references that
never moves into the answer would each fail every answer, so the ask refuses them, naming where the
reference sits. The schema is one resource: a ``$id`` below the root, or an anchor name declared
twice, is refused too. A reference into the schema itself (a pointer, an anchor, an absolute reference
under the root's ``$id``) or into a JSON Schema meta-schema stays accepted, and the answer check
agrees with the ask on it.
"""

from __future__ import annotations

import contextlib
import enum
import re
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast

import pytest
import referencing.exceptions
from pydantic import BaseModel
from tai42_contract.channels import Channel
from tai42_contract.interactions import AnswerFormat, AnswerMismatchError, QuestionFormat

from tai42_skeleton.interactions import ask
from tai42_skeleton.interactions.answer_check import check_answer, schema_mismatch
from tai42_skeleton.interactions.ask.payload import normalize_schema
from tai42_skeleton.interactions.ask.validate import _validate_channel_form
from tai42_skeleton.interactions.form_schema import validate_channel_form_schema, validate_form_answer_schema
from tai42_skeleton.interactions.form_shapes import scan_form_shapes
from tai42_skeleton.interactions.schema_rules import check_answer_schema_rules

_REMOTE = "it names a schema outside this one, and no schema is fetched — inline it or move it under $defs"
_FAR = "https://example.invalid/s.json"
_NOT_A_SCHEMA = "points at a value that is not a schema"
_CYCLE = (
    "forms a cycle of references that never descends into the instance, so checking an instance that follows it "
    "would never end"
)
_ONE_RESOURCE = (
    "below the root is not allowed: the schema is one resource, so every reference resolves inside it"
    " — drop the $id and point references at '#/…' or at an anchor"
)

_M20 = "https://json-schema.org/draft/2020-12/schema"
_REENTRY = (
    "breaks the JSON Schema rules at $defs/x/$dynamicAnchor: $dynamicAnchor 'meta' below the root is not allowed "
    "when the schema has an $id and refers into a JSON Schema meta-schema: the meta-schema's $dynamicRef '#meta' "
    "would re-enter the schema here and resolve its references against the meta-schema — rename the anchor"
)
_X_TO_Y = {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}}
# A meta-schema reference beside a meta-schema's dynamic anchor name below a root ``$id``: the
# meta-schema's ``$dynamicRef`` would re-enter the schema at ``$defs/x``.
_REENTERED_ROOT = {"$id": "https://e.com/r", "$ref": _M20, "$defs": _X_TO_Y}
_REENTERED_FORM = {
    "$id": "https://e.com/r",
    "type": "object",
    "properties": {"a": {"type": "string", "$ref": _M20}},
    "$defs": _X_TO_Y,
}
_META_ANCHOR = f"{_M20}#meta"
_VALIDATION_ANCHOR = "https://json-schema.org/draft/2020-12/meta/validation#meta"


def _entered(keyword: str, ref: str) -> str:
    return (
        "breaks the JSON Schema rules at $defs/x/$dynamicAnchor: $dynamicAnchor 'meta' below the root is not allowed "
        f"when the schema has an $id and refers into a JSON Schema meta-schema: {keyword} {ref!r} names the "
        "meta-schema's $dynamicAnchor 'meta', which would re-enter the schema here and resolve its references "
        "against the meta-schema — rename the anchor"
    )


# A reference naming a meta-schema's own dynamic anchor, beside an anchor of that name below a root
# ``$id``: the lookup enters the meta-schema and comes back into the schema at ``$defs/x``.
_ENTERED_FREE = {"$id": "https://e.com/r", "$ref": _META_ANCHOR, "$defs": _X_TO_Y}
_ENTERED_DYNAMIC = {"$id": "https://e.com/r", "$dynamicRef": _META_ANCHOR, "$defs": _X_TO_Y}
_ENTERED_VALIDATION = {"$id": "https://e.com/r", "$ref": _VALIDATION_ANCHOR, "$defs": _X_TO_Y}
_ENTERED_BARE = {"$id": "https://e.com/r", "$ref": _META_ANCHOR, "$defs": {"x": {"$dynamicAnchor": "meta"}}}
_ENTERED_FORM = {
    "$id": "https://e.com/r",
    "type": "object",
    "properties": {"a": {"type": "string", "$ref": _META_ANCHOR}},
    "$defs": _X_TO_Y,
}
_NOT_AN_INDEX = (
    "property 'a' breaks the JSON Schema rules: $ref '#/$defs/s/allOf/-1' indexes an array with '-1', which is not "
    "a JSON Pointer array index: write the index as a whole number like 0 or 2"
)
_NEGATIVE_INDEX = {
    "type": "object",
    "properties": {"a": {"type": "string", "$ref": "#/$defs/s/allOf/-1"}},
    "$defs": {"s": {"allOf": [{"maxLength": 3}]}},
}


_APPLICATOR = "https://json-schema.org/draft/2020-12/meta/applicator"
_CORE = "https://json-schema.org/draft/2020-12/meta/core"


def _taken(uri: str) -> str:
    return (
        f"breaks the JSON Schema rules at $id: $id {uri!r} names a JSON Schema meta-schema: a schema cannot take a "
        "meta-schema's identifier — give it an identifier of its own, or drop the $id"
    )


# A schema under a meta-schema's identifier, which the answer check would put in that meta-schema's place.
_TAKEN_FREE = {"$id": _APPLICATOR, "$ref": _M20}
_TAKEN_PROPERTY = {"$id": _APPLICATOR, "properties": {"a": {"$ref": _M20}}}
_TAKEN_OWN_POINTER = {"$id": _M20, "properties": {"a": {"$ref": "#/$defs/s"}}, "$defs": {"s": {"type": "string"}}}
_TAKEN_BESIDE_AN_ANCHOR = {"$id": _M20, "$defs": _X_TO_Y, "$ref": _APPLICATOR}
_TAKEN_FORM = {"$id": _APPLICATOR, "type": "object", "properties": {"a": {"type": "string"}}}


def _prop(value: dict[str, Any], **root: Any) -> dict[str, Any]:
    return {"type": "object", "properties": {"a": value}, **root}


_REFUSED: list[tuple[dict[str, Any], str]] = [
    (
        {"$ref": "#/nope"},
        "{s} breaks the JSON Schema rules at $ref: $ref '#/nope' does not resolve: nothing at '/nope'",
    ),
    (
        {"$ref": "#foo"},
        "{s} breaks the JSON Schema rules at $ref: $ref '#foo' does not resolve: no $anchor or $dynamicAnchor 'foo'",
    ),
    (
        {"$ref": "#a/b"},
        "{s} breaks the JSON Schema rules at $ref: $ref '#a/b' does not resolve: 'a/b' is not an anchor name",
    ),
    (
        {"$ref": "https://example.invalid/s.json"},
        f"{{s}} breaks the JSON Schema rules at $ref: $ref '{_FAR}' does not resolve: {_REMOTE}",
    ),
    (
        {"$id": "https://example.com/root.json", "$ref": "other.json"},
        f"{{s}} breaks the JSON Schema rules at $ref: $ref 'other.json' does not resolve: {_REMOTE}",
    ),
    (
        _prop({"$id": "https://example.com/a.json", "$ref": "#/$defs/x"}, **{"$defs": {"x": {"type": "string"}}}),
        f"{{s}} property 'a' breaks the JSON Schema rules: $id 'https://example.com/a.json' {_ONE_RESOURCE}",
    ),
    (
        _prop({"$id": "https://example.com/a.json", "$defs": {"x": {"type": "string"}}, "$ref": "#/$defs/x"}),
        f"{{s}} property 'a' breaks the JSON Schema rules: $id 'https://example.com/a.json' {_ONE_RESOURCE}",
    ),
    (
        {"properties": {"z": {"not": {"$id": "https://example.com/n/", "$ref": "#/$defs/q"}}}},
        f"{{s}} property 'z' breaks the JSON Schema rules: $id 'https://example.com/n/' {_ONE_RESOURCE}",
    ),
    (
        {"$defs": {"a": {"$anchor": "t"}, "b": {"$anchor": "t"}}, "$ref": "#t"},
        "{s} breaks the JSON Schema rules at $defs/b/$anchor: anchor 't' ($anchor) is declared more than once,"
        " so '#t' could resolve to either",
    ),
    (
        {"$dynamicRef": "#/nope"},
        "{s} breaks the JSON Schema rules at $dynamicRef: $dynamicRef '#/nope' does not resolve: nothing at '/nope'",
    ),
    (
        _prop({"type": "string", "pattern": "a", "$ref": "#/properties/a/pattern"}),
        f"{{s}} property 'a' breaks the JSON Schema rules: $ref '#/properties/a/pattern' {_NOT_A_SCHEMA}",
    ),
    (
        _prop({"type": "integer", "minimum": 3, "$ref": "#/properties/a/minimum"}),
        f"{{s}} property 'a' breaks the JSON Schema rules: $ref '#/properties/a/minimum' {_NOT_A_SCHEMA}",
    ),
    (
        _prop({"type": "string", "$ref": "#/required/0"}, required=["a"]),
        "{s} property 'a' breaks the JSON Schema rules: $ref '#/required/0' points at a value that is not a schema",
    ),
    (
        {"x-foo": {"pattern": "["}, "$ref": "#/x-foo"},
        "{s} breaks the JSON Schema rules at x-foo/pattern: '[' is not a 'regex'",
    ),
    (
        {"type": "string", "enum": [{"pattern": "["}], "$ref": "#/enum/0"},
        "{s} breaks the JSON Schema rules at enum/0/pattern: '[' is not a 'regex'",
    ),
    ({"$ref": "#"}, f"{{s}} breaks the JSON Schema rules at $ref: $ref '#' {_CYCLE}"),
    (
        {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"},
        f"{{s}} breaks the JSON Schema rules at $defs/b/$ref: $ref '#/$defs/a' {_CYCLE}",
    ),
    (
        _prop({"type": "string", "allOf": [{"$ref": "#/properties/a"}]}),
        f"{{s}} property 'a' breaks the JSON Schema rules: $ref '#/properties/a' {_CYCLE}",
    ),
    (
        {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/nope"}}, "$ref": "#/$defs/a"},
        "{s} breaks the JSON Schema rules at $defs/b/$ref: $ref '#/nope' does not resolve: nothing at '/nope'",
    ),
    (
        _prop({"type": "string", "$ref": "https://example.invalid/s.json"}),
        f"{{s}} property 'a' breaks the JSON Schema rules: $ref '{_FAR}' does not resolve: {_REMOTE}",
    ),
    (
        _prop({"type": "string", "$ref": "#/nope"}),
        "{s} property 'a' breaks the JSON Schema rules: $ref '#/nope' does not resolve: nothing at '/nope'",
    ),
    (
        {"default": ({"$ref": "#/nope"},), "properties": {"a": {"$ref": "#/default/0"}}},
        "{s} breaks the JSON Schema rules at default/0/$ref: $ref '#/nope' does not resolve: nothing at '/nope'",
    ),
    (
        {"default": ({"$ref": "#/default/0"},), "$ref": "#/default/0"},
        f"{{s}} breaks the JSON Schema rules at default/0/$ref: $ref '#/default/0' {_CYCLE}",
    ),
    (_REENTERED_ROOT, f"{{s}} {_REENTRY}"),
    (_REENTERED_FORM, f"{{s}} {_REENTRY}"),
    (_ENTERED_FREE, f"{{s}} {_entered('$ref', _META_ANCHOR)}"),
    (_ENTERED_DYNAMIC, f"{{s}} {_entered('$dynamicRef', _META_ANCHOR)}"),
    (_ENTERED_VALIDATION, f"{{s}} {_entered('$ref', _VALIDATION_ANCHOR)}"),
    (_ENTERED_BARE, f"{{s}} {_entered('$ref', _META_ANCHOR)}"),
    (_ENTERED_FORM, f"{{s}} {_entered('$ref', _META_ANCHOR)}"),
    (_NEGATIVE_INDEX, f"{{s}} {_NOT_AN_INDEX}"),
    (_TAKEN_FREE, f"{{s}} {_taken(_APPLICATOR)}"),
    (_TAKEN_PROPERTY, f"{{s}} {_taken(_APPLICATOR)}"),
    (_TAKEN_OWN_POINTER, f"{{s}} {_taken(_M20)}"),
    (_TAKEN_BESIDE_AN_ANCHOR, f"{{s}} {_taken(_M20)}"),
    (_TAKEN_FORM, f"{{s}} {_taken(_APPLICATOR)}"),
]

_CHECKS: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "answer": (check_answer_schema_rules, "answer schema"),
    "form": (validate_form_answer_schema, "form schema"),
}


@pytest.mark.parametrize("check", list(_CHECKS))
@pytest.mark.parametrize(("schema", "message"), _REFUSED)
def test_ask_refuses_a_reference_the_answer_check_cannot_follow(check, schema, message):
    run, subject = _CHECKS[check]
    with pytest.raises(ValueError, match=re.escape(message.format(s=subject))):
        run(schema)


_CHANNEL_REFUSED = [(schema, message) for schema, message in _REFUSED if schema.get("type") == "object"]


@pytest.mark.parametrize(("schema", "message"), _CHANNEL_REFUSED)
def test_a_channel_form_refuses_a_reference_beside_a_type(schema, message):
    if "$id" in schema["properties"]["a"]:
        pytest.skip("the channel subset refuses a property without a type first")
    with pytest.raises(ValueError, match=re.escape(message.format(s="form schema"))):
        validate_channel_form_schema(schema)


class _Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class _Inner(BaseModel):
    n: int


class _Model(BaseModel):
    color: _Color
    inner: _Inner


_D7 = "http://json-schema.org/draft-07/schema#"
_ACCEPTED: list[dict[str, Any]] = [
    {"$defs": {"x": {"type": "string"}}, "$ref": "#/$defs/x"},
    {"$defs": {"x": {"$anchor": "x", "type": "string"}}, "$ref": "#x"},
    {"$dynamicAnchor": "node", "type": "object", "properties": {"c": {"$dynamicRef": "#node"}}},
    {
        "$id": "https://example.com/root.json",
        "$ref": "https://example.com/root.json#/$defs/x",
        "$defs": {"x": {"type": "string"}},
    },
    {"$schema": _D7, "definitions": {"x": {"type": "string"}}, "$ref": "#/definitions/x"},
    {"$ref": "https://json-schema.org/draft/2020-12/schema"},
    _prop({"$ref": "#"}),
    normalize_schema(_Model),
    {
        "$id": "https://e.com/r",
        "$dynamicAnchor": "meta",
        "$ref": _M20,
        "properties": {"x-k": {"$ref": "#/$defs/y"}},
        "$defs": {"y": {"type": "string"}},
    },
    {"$ref": _M20, "properties": {"x-k": {"$ref": "#/$defs/x"}}, "$defs": _X_TO_Y},
    {
        "$id": "https://e.com/r",
        "$ref": _M20,
        "properties": {"x-k": {"$dynamicRef": "#t"}},
        "$defs": {"x": {"$dynamicAnchor": "t", "$ref": "#/$defs/y"}, "y": {"type": "string"}},
    },
    # A meta-schema that never refers to the anchor name it declares binds nothing dynamically.
    {"$id": "https://e.com/r", "$ref": _D7, "$defs": _X_TO_Y},
    {"$id": "https://e.com/r", "$ref": "https://json-schema.org/draft/2020-12/meta/validation", "$defs": _X_TO_Y},
    {
        "$id": "https://e.com/r",
        "$ref": "https://json-schema.org/draft/2020-12/meta/validation#/$defs/nonNegativeInteger",
        "$defs": _X_TO_Y,
    },
    # A meta-schema subschema that refers to nothing binds nothing dynamically.
    {"$id": "https://e.com/r", "properties": {"a": {"$ref": f"{_CORE}#/$defs/anchorString"}}, "$defs": _X_TO_Y},
    {"$id": "https://e.com/r", "properties": {"a": {"$ref": f"{_M20}#/$vocabulary"}}, "$defs": _X_TO_Y},
]


@pytest.mark.parametrize("check", list(_CHECKS))
@pytest.mark.parametrize("schema", _ACCEPTED)
def test_ask_admits_a_reference_resolving_inside_the_schema(check, schema):
    _CHECKS[check][0](schema)


def test_a_channel_form_admits_a_reference_beside_a_type():
    validate_channel_form_schema(_prop({"type": "string", "$ref": "#/$defs/s"}, **{"$defs": {"s": {"maxLength": 5}}}))


def _answers() -> list[Any]:
    return [
        "x",
        1,
        None,
        [],
        {"a": "x"},
        {"a": {"a": 1}},
        {"color": "red", "inner": {"n": 1}},
        {"properties": {"a": {}}},
        {"properties": {"a": {"x-k": 1}}},
    ]


@pytest.mark.parametrize("schema", _ACCEPTED)
def test_the_answer_check_agrees_with_the_ask_on_every_accepted_reference(schema):
    for fmt in (AnswerFormat.FREE, AnswerFormat.EXTERNAL):
        question = QuestionFormat(answer_format=fmt, format_payload={"schema": schema})
        for answer in _answers():
            with contextlib.suppress(AnswerMismatchError):
                check_answer(question, answer)


def test_a_pydantic_answer_outside_its_enum_names_the_field():
    question = QuestionFormat(answer_format=AnswerFormat.FREE, format_payload={"schema": normalize_schema(_Model)})
    with pytest.raises(AnswerMismatchError, match=re.escape("'green' is not one of ['red', 'blue']")) as caught:
        check_answer(question, {"color": "green", "inner": {"n": 1}})
    assert caught.value.field == "color"


@pytest.fixture
def hits() -> Iterator[tuple[str, list[str]]]:
    # A local HTTP server serving a schema at any path, recording every request it is sent.
    seen: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            body = b'{"type": "integer", "minimum": 10}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/s.json", seen
    finally:
        server.shutdown()
        server.server_close()


def test_ask_refuses_a_remote_reference_without_fetching_it(hits):
    url, seen = hits
    with pytest.raises(ValueError, match=re.escape(f"$ref '{url}' does not resolve: {_REMOTE}")):
        check_answer_schema_rules({"$ref": url})
    assert seen == []


def test_the_free_answer_check_never_fetches_a_remote_reference(hits):
    url, seen = hits
    with pytest.raises(referencing.exceptions.Unresolvable):
        schema_mismatch("x", {"$ref": url})
    assert seen == []


def test_the_form_answer_check_never_fetches_a_remote_reference(hits):
    url, seen = hits
    question = QuestionFormat(
        answer_format=AnswerFormat.FORM, format_payload={"schema": _prop({"type": "string", "$ref": url})}
    )
    with pytest.raises(
        ValueError,
        match=re.escape(
            "question schema is invalid: form schema property 'a' breaks the JSON Schema rules: "
            f"$ref '{url}' does not resolve: "
        ),
    ):
        scan_form_shapes(question, {"a": "x"})
    assert seen == []


class _FormChannel:
    supports_form_delivery = True


_NOWHERE = _prop({"type": "string", "$ref": "#/nope"})
_NOWHERE_TAIL = "property 'a' breaks the JSON Schema rules: $ref '#/nope' does not resolve: nothing at '/nope'"
_SPLIT = _prop({"type": "string", "$id": "https://example.com/a.json"})
_SPLIT_TAIL = f"property 'a' breaks the JSON Schema rules: $id 'https://example.com/a.json' {_ONE_RESOURCE}"
_DOOR_REFUSED = [
    (_NOWHERE, _NOWHERE_TAIL),
    (_SPLIT, _SPLIT_TAIL),
    (_REENTERED_FORM, _REENTRY),
    (_ENTERED_FORM, _entered("$ref", _META_ANCHOR)),
    (_NEGATIVE_INDEX, _NOT_AN_INDEX),
    (_TAKEN_FORM, _taken(_APPLICATOR)),
]
_LATER = datetime.now(UTC) + timedelta(hours=1)


@pytest.mark.parametrize(("schema", "tail"), _DOOR_REFUSED)
@pytest.mark.parametrize(
    ("extra", "subject"),
    [
        ({"answer_format": "free", "to": "caller", "mode": "async", "expiry_at": _LATER}, "answer schema"),
        ({"answer_format": "external", "link": "{callback_url}"}, "answer schema"),
        ({"answer_format": "form"}, "form schema"),
    ],
)
async def test_every_ask_door_refuses_a_schema_the_answer_check_cannot_use(extra, subject, schema, tail):
    with pytest.raises(ValueError, match=re.escape(f"{subject} {tail}")):
        await ask("q", schema=schema, **extra)


@pytest.mark.parametrize(("schema", "tail"), _DOOR_REFUSED)
def test_a_channel_form_ask_refuses_a_schema_the_answer_check_cannot_use(schema, tail):
    with pytest.raises(ValueError, match=re.escape(f"form schema {tail}")):
        _validate_channel_form("wa", cast("Channel", _FormChannel()), schema, "q", None)


# Each schema names its scopes with a ``$id`` below the root, which the one-resource rule refuses
# before any reference is followed.
_DYNAMIC_SCOPES: dict[str, Any] = {
    "$id": "urn:root",
    "type": "object",
    "properties": {"p": {"$ref": "urn:r1"}, "q": {"$ref": "urn:r2"}},
    "$defs": {
        "r1": {"$id": "urn:r1", "$dynamicAnchor": "m", "properties": {"k": {"$ref": "urn:b"}}},
        "r2": {"$id": "urn:r2", "$dynamicAnchor": "m", "$ref": "urn:b"},
        "b": {"$id": "urn:b", "allOf": [{"$dynamicRef": "urn:c#m"}]},
        "c": {"$id": "urn:c", "$dynamicAnchor": "m", "type": "string"},
    },
}
_RECURSIVE_SCOPES: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2019-09/schema",
    "$id": "urn:root",
    "type": "object",
    "properties": {"p": {"$ref": "urn:r1"}, "q": {"$ref": "urn:r2"}},
    "$defs": {
        "r1": {"$id": "urn:r1", "$recursiveAnchor": True, "properties": {"k": {"$ref": "urn:b"}}},
        "r2": {"$id": "urn:r2", "$recursiveAnchor": True, "$ref": "urn:b"},
        "b": {"$id": "urn:b", "$recursiveAnchor": True, "allOf": [{"$recursiveRef": "#"}]},
    },
}


@pytest.mark.parametrize("schema", [_DYNAMIC_SCOPES, _RECURSIVE_SCOPES])
def test_a_schema_naming_its_scopes_with_ids_below_the_root_is_refused(schema):
    with pytest.raises(ValueError, match=re.escape(f"at $defs/r1/$id: $id 'urn:r1' {_ONE_RESOURCE}")):
        check_answer_schema_rules(schema)
