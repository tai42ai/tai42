"""The OpenAPI 3.1 emitter — turns the route-metadata registry into a spec.

The app is a FastMCP + Starlette ``custom_route`` server, so it emits no schema
of its own. This module walks the shared route-enumeration primitive
(:func:`tai42_skeleton.app.route_registry.load_api_routes`) and builds a valid
OpenAPI 3.1 document: one operation per method, api-key ``security`` for authed
routes, request bodies and query parameters described in pydantic's VALIDATION
mode (what a client may SEND), responses described in pydantic's SERIALIZATION
mode (what the server EMITS — a ``@computed_field`` and a field the server always
writes are present and required), and responses that wrap the ``{"data": ...}``
success envelope and the ``{"error": ...}`` failure envelope.

The two modes are generated together in a single :func:`pydantic.json_schema.models_json_schema`
pass over every ``(model, mode)`` pair the routes reference. A model that appears in
both roles with schemas that differ between the modes is split into pydantic's
``-Input`` / ``-Output`` components automatically, and each ``$ref`` resolves to the
component for its role; a model used in one mode keeps its bare name.

The two sources of a ``503`` are kept apart, because they answer with different
bodies. A route's ``error_statuses`` are the statuses it answers with the plain
``{"error": ...}`` envelope, so a declared ``503`` (an operation's
``UnavailableError``) documents that envelope. The reload gate is the OTHER source:
it is declared as ``reload_gated`` and this module owns its response entirely — the
constant-message ``ReloadingError`` body plus the ``Retry-After`` header. A route
carrying BOTH publishes a ``503`` admitting either body.

Emission is OFFLINE by construction: the registry is populated purely by
importing the router modules, so no database, Redis, or live config/manifest is
required. The docs pipeline emits the spec with no environment booted, so
emission must never need one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from importlib.metadata import version
from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import JsonSchemaMode, models_json_schema

from tai42_skeleton.app.reload_gate import REJECT_MESSAGE
from tai42_skeleton.app.route_registry import RouteMetadata, load_api_routes, method_to_action

_SECURITY_SCHEME = "ApiKeyAuth"
_API_KEY_HEADER = "x-api-key"

# The literal prefix of every component ``$ref`` target the document carries.
_REF_PREFIX = "#/components/schemas/"

# The pydantic ``$ref`` target for every generated component schema.
_REF_TEMPLATE = _REF_PREFIX + "{model}"

# A request body or query parameter describes what a client may SEND; a response
# describes what the server EMITS.
_VALIDATION: JsonSchemaMode = "validation"
_SERIALIZATION: JsonSchemaMode = "serialization"

# Shared response-envelope component schemas.
_ERROR_SCHEMA = "Error"
_RELOADING_ERROR_SCHEMA = "ReloadingError"

_PATH_PARAM = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")

_NON_JSON_DESCRIPTIONS: dict[str, str] = {
    "text/event-stream": "Server-sent event stream.",
    "text/csv": "CSV export.",
    "application/octet-stream": "Asset bytes.",
    "text/html": "HTML page.",
}

_STATUS_DESCRIPTIONS: dict[int, str] = {
    400: "Malformed request.",
    401: "Missing or invalid api key.",
    403: "Forbidden.",
    404: "Resource not found.",
    409: "Conflict with the current resource state.",
    410: "Resource no longer available.",
    413: "Request body too large.",
    415: "Unsupported media type.",
    422: "Request failed validation.",
    500: "Internal server error.",
    503: "A dependency this route needs is temporarily unavailable; retry shortly.",
}

# The reload gate's own ``503`` — a different body from the typed ``503`` above, so
# it carries its own description. A route that answers both publishes the combined
# one.
_RELOADING_DESCRIPTION = "The server is applying a config reload; retry shortly."
_RELOADING_OR_UNAVAILABLE_DESCRIPTION = (
    "The server is applying a config reload, or a dependency this route needs is "
    "temporarily unavailable; retry shortly."
)


def _openapi_path(path: str) -> str:
    """Rewrite Starlette path params to OpenAPI form, dropping the ``:path`` converter suffix.

    ``/x/{p:path}`` -> ``/x/{p}``.
    """
    return _PATH_PARAM.sub(lambda m: "{" + m.group(1) + "}", path)


def _path_parameters(path: str) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        }
        for name in _PATH_PARAM.findall(path)
    ]


def _operation_id(method: str, path: str) -> str:
    slug = _PATH_PARAM.sub(lambda m: m.group(1), path)
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", slug).strip("_")
    return f"{method.lower()}_{slug}"


# Envelope schema names the emitter reserves for the shared ``{"error": ...}`` and
# reloading responses; a request/response model may never claim one.
_RESERVED_SCHEMA_NAMES = frozenset({_ERROR_SCHEMA, _RELOADING_ERROR_SCHEMA})


def _assign_component(components: dict[str, Any], name: str, schema: dict[str, Any]) -> None:
    """Write ``schema`` under ``name`` in ``components``, raising LOUDLY on a name collision.

    A silent keep or overwrite would otherwise land the wrong schema. The collision cases: a
    reserved envelope name, or two distinct models (or ``$defs``) sharing a ``__name__`` with
    differing schemas. Re-registering an identical schema (the same model reached twice) is a no-op.
    """
    if name in _RESERVED_SCHEMA_NAMES:
        raise ValueError(f"schema name {name!r} collides with a reserved response-envelope component")
    existing = components.get(name)
    if existing is not None and existing != schema:
        raise ValueError(f"schema name {name!r} maps to two distinct schemas — a component-name collision")
    components[name] = schema


class _ComponentSchemas:
    """The role-correct component ``$ref`` for a model, from one shared generation.

    Built by :func:`_component_schemas` from every ``(model, mode)`` pair the routes
    reference through a single :func:`models_json_schema` call. ``ref`` returns
    the ``{"$ref": ...}`` for a model in a given mode — pointing at its ``-Input`` /
    ``-Output`` component when the two modes differ, or its bare name when they do not.
    ``validation_schema`` returns a model's validation-mode definition, from which the
    query-parameter builder reads properties and required flags.
    """

    def __init__(
        self,
        refs: Mapping[tuple[Any, JsonSchemaMode], dict[str, Any]],
        definitions: dict[str, dict[str, Any]],
    ) -> None:
        self._refs = dict(refs)
        self._definitions = definitions

    def ref(self, model: type[BaseModel], mode: JsonSchemaMode) -> dict[str, Any]:
        return dict(self._refs[(model, mode)])

    def validation_schema(self, model: type[BaseModel]) -> dict[str, Any]:
        name = self._refs[(model, _VALIDATION)]["$ref"].rsplit("/", 1)[-1]
        return self._definitions[name]


def _route_model_modes(
    metas: Iterable[RouteMetadata],
) -> set[tuple[type[BaseModel], JsonSchemaMode]]:
    """Every ``(model, mode)`` pair the routes reference.

    A request body and a query model describe what a client SENDS, so they are collected
    in VALIDATION mode; a response model describes what the server EMITS, so it is
    collected in SERIALIZATION mode.
    """
    pairs: set[tuple[type[BaseModel], JsonSchemaMode]] = set()
    for meta in metas:
        if meta.response_model is not None:
            pairs.add((meta.response_model, _SERIALIZATION))
        if meta.request_model is not None:
            pairs.add((meta.request_model, _VALIDATION))
        if meta.query_model is not None:
            pairs.add((meta.query_model, _VALIDATION))
    return pairs


def _component_schemas(
    pairs: Iterable[tuple[type[BaseModel], JsonSchemaMode]], components: dict[str, Any]
) -> _ComponentSchemas:
    """Generate the given ``(model, mode)`` component schemas in one pass and merge them.

    The pairs are sorted by ``(qualname, mode)`` for byte-stable output, then produced
    through a single :func:`models_json_schema` call so a model used in both modes gets
    pydantic's ``-Input`` / ``-Output`` split and every ``$ref`` resolves. Each definition is
    merged into ``components`` through :func:`_assign_component`, so a model that claims a
    reserved envelope name still raises LOUDLY.
    """
    ordered = sorted(set(pairs), key=lambda pair: (pair[0].__qualname__, pair[1]))
    mapping, definitions = models_json_schema(ordered, ref_template=_REF_TEMPLATE)
    defs = definitions.get("$defs", {})
    for name, schema in defs.items():
        _assign_component(components, name, schema)
    return _ComponentSchemas(mapping, defs)


_NULL_BRANCH = {"type": "null"}


def _query_schema(prop_schema: dict[str, Any]) -> dict[str, Any]:
    """A field's JSON schema as a QUERY parameter's schema, with nullability stripped.

    A query string carries no JSON ``null``: a client omits a parameter, it never sends one
    valued null. So pydantic's rendering of ``T | None`` — ``anyOf [T, null]`` plus
    ``default: null`` — is collapsed to plain ``T`` with no default, and a union of several
    real branches keeps its ``anyOf`` minus the null branch. Sibling keywords (``title``,
    ``description``, bounds) survive the collapse. A non-nullable schema passes through.
    """
    schema = dict(prop_schema)
    branches = schema.get("anyOf")
    if isinstance(branches, list) and _NULL_BRANCH in branches:
        real = [branch for branch in branches if branch != _NULL_BRANCH]
        del schema["anyOf"]
        if len(real) == 1:
            schema = {**real[0], **schema}
        else:
            schema["anyOf"] = real
    if "default" in schema and schema["default"] is None:
        del schema["default"]
    return schema


def _query_parameters(model: type[BaseModel], schemas: _ComponentSchemas) -> list[dict[str, Any]]:
    """A model's fields as ``in: query`` parameters — a read method's ``request_model``, or any ``query_model``.

    A model whose fields are query inputs (a GET reading its inputs from the query string,
    or a door declaring a ``query_model``) turns each field into a query parameter, never a
    request body. Each parameter carries the field's own JSON schema — a ``list[…]`` field
    keeps its ``array`` schema, published as a repeated ``?name=`` param under OpenAPI's
    default form serialization — stripped of nullability by :func:`_query_schema`, and is
    ``required`` exactly when the model marks it required (a field with a default is
    optional). The field's description rides on the PARAMETER, not on its schema: it is the
    Parameter Object's own ``description`` that a generator renders, so it is moved there
    rather than left where only a schema-aware reader would find it. The model's
    validation-mode schema is read from the shared definitions, so any ``$defs`` a field
    schema references are already merged into ``components`` and the ``$ref``s resolve.
    """
    schema = schemas.validation_schema(model)
    required = set(schema.get("required", []))
    parameters: list[dict[str, Any]] = []
    for name, prop_schema in schema.get("properties", {}).items():
        param_schema = _query_schema(prop_schema)
        parameter: dict[str, Any] = {"name": name, "in": "query"}
        description = param_schema.pop("description", None)
        if description is not None:
            parameter["description"] = description
        parameter["required"] = name in required
        parameter["schema"] = param_schema
        parameters.append(parameter)
    return parameters


def _check_unique_parameters(parameters: list[dict[str, Any]], *, path: str, method: str) -> None:
    """Refuse a route whose assembled parameters repeat a ``(name, in)`` pair.

    OpenAPI forbids the duplicate, and the sources can collide unseen: a path param named
    like a model field, a read door's ``request_model`` overlapping its ``query_model``, or
    two fields aliased to one query key. Emitting it would ship an invalid document, so it
    fails the emission LOUDLY naming the route and the parameter.
    """
    seen: set[tuple[str, str]] = set()
    for parameter in parameters:
        key = (parameter["name"], parameter["in"])
        if key in seen:
            raise ValueError(
                f"{method} {path} declares parameter {key[0]!r} in {key[1]} twice — "
                "the path, the request_model, and the query_model must not claim the same name"
            )
        seen.add(key)


def _json_envelope_schema(meta: RouteMetadata, schemas: _ComponentSchemas) -> dict[str, Any]:
    if meta.response_model is None:
        data_schema: dict[str, Any] = {}
    else:
        data_schema = schemas.ref(meta.response_model, _SERIALIZATION)
    return {
        "type": "object",
        "properties": {"data": data_schema},
        "required": ["data"],
    }


def _json_body_schema(meta: RouteMetadata, schemas: _ComponentSchemas) -> dict[str, Any]:
    """The ``application/json`` success body schema.

    An enveloped route (the default) wraps its model in ``{"data": <model>}``. A route
    declared ``enveloped=False`` answers a RAW top-level body, so its model is the body
    schema DIRECTLY — a ``$ref`` with no ``data`` wrapper. An unwrapped route always
    carries a ``response_model`` (the registration guard enforces it); a missing one
    here is a broken registration, raised LOUDLY rather than emitting an empty body.
    """
    if meta.enveloped:
        return _json_envelope_schema(meta, schemas)
    if meta.response_model is None:
        raise ValueError(f"route {meta.path} declares enveloped=False without a response_model")
    return schemas.ref(meta.response_model, _SERIALIZATION)


def _success_response(meta: RouteMetadata, method: str, schemas: _ComponentSchemas) -> dict[str, Any]:
    """The 200/2xx response for ``method``, documenting every content type it serves.

    ``application/json`` carries the ``{"data": ...}`` envelope by default, or the
    model's schema DIRECTLY when the route is declared ``enveloped=False`` (a raw
    top-level body); a streaming, CSV, HTML, or asset/download type answers its own
    media type instead. A method that serves more than one type (the runs export: CSV
    or a JSON download) lists them all under ``content``.

    A route with no typed body carries a ``response_model`` of ``None`` and a
    ``no_body_reason`` (the registration guard enforces the pairing): the reason
    becomes the response ``description`` and rides an ``x-no-body`` extension, so the
    absence of a ``{"data": <model>}`` schema is a declared, described exception
    rather than a silent empty ``data``.
    """
    media_types = meta.success_media_types[method]
    content: dict[str, Any] = {}
    for media_type in media_types:
        if media_type == "application/json":
            content[media_type] = {"schema": _json_body_schema(meta, schemas)}
        else:
            content[media_type] = {"schema": {"type": "string"}}
    no_body_reason = meta.response_model is None and meta.no_body_reason
    if no_body_reason:
        description = no_body_reason
    elif len(media_types) == 1 and media_types[0] != "application/json":
        description = _NON_JSON_DESCRIPTIONS.get(media_types[0], "Success.")
    else:
        description = "Success."
    response: dict[str, Any] = {"description": description, "content": content}
    if no_body_reason:
        response["x-no-body"] = no_body_reason
    return response


def _error_response(status: int) -> dict[str, Any]:
    """The response for a status the route answers with the plain ``{"error": ...}`` envelope.

    Covers every entry of ``error_statuses``, the declared ``503`` included.
    """
    return {
        "description": _STATUS_DESCRIPTIONS.get(status, "Error."),
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{_ERROR_SCHEMA}"}}},
    }


def _reload_gate_response(*, also_unavailable: bool) -> dict[str, Any]:
    """The reload gate's ``503``: the constant-message ``ReloadingError`` body plus the ``Retry-After`` header.

    The ``Retry-After`` header is the one the gate stamps.
    ``also_unavailable`` marks a route that ALSO answers a typed ``UnavailableError``
    ``503`` with the plain ``{"error": ...}`` envelope; its one ``503`` slot must then
    admit either body, and ``Retry-After`` rides only the reloading half.

    The combined schema is ``anyOf``, not ``oneOf``: ``Error`` is open (it constrains
    only ``error``), so a reloading body satisfies BOTH branches — under ``oneOf``,
    which demands exactly one match, the gate's own response would fail its own spec.
    """
    reloading: dict[str, Any] = {"$ref": f"#/components/schemas/{_RELOADING_ERROR_SCHEMA}"}
    if also_unavailable:
        schema: dict[str, Any] = {"anyOf": [reloading, {"$ref": f"#/components/schemas/{_ERROR_SCHEMA}"}]}
        description = _RELOADING_OR_UNAVAILABLE_DESCRIPTION
        header_description = "Seconds to wait before retrying; carried by the reloading answer."
    else:
        schema = reloading
        description = _RELOADING_DESCRIPTION
        header_description = "Seconds to wait before retrying."
    return {
        "description": description,
        "headers": {"Retry-After": {"description": header_description, "schema": {"type": "integer"}}},
        "content": {"application/json": {"schema": schema}},
    }


def _operation_responses(meta: RouteMetadata, method: str, schemas: _ComponentSchemas) -> dict[str, Any]:
    """The operation's ``responses`` map.

    Covers the success status, any additional success statuses, the plain-envelope error statuses,
    and — merged into the ``503`` slot, OVERWRITING a declared 503 so one slot admits both bodies —
    the reload gate's response when the route is ``reload_gated``.
    """
    responses: dict[str, Any] = {str(meta.success_status): _success_response(meta, method, schemas)}
    for status in meta.additional_success_statuses:
        responses[str(status)] = _success_response(meta, method, schemas)
    for status in meta.error_statuses:
        responses[str(status)] = _error_response(status)
    if meta.reload_gated:
        responses["503"] = _reload_gate_response(also_unavailable=503 in meta.error_statuses)
    return responses


def _operation_parameters(meta: RouteMetadata, method: str, schemas: _ComponentSchemas) -> list[dict[str, Any]]:
    """The operation's parameters.

    Path params, then a read method's ``request_model`` fields as ``in: query`` (a GET reads its
    inputs from the query string, never a body), then any ``query_model`` fields (``in: query`` for
    ANY method). Refuses a duplicate ``(name, in)`` pair before returning.
    """
    parameters = _path_parameters(meta.path)
    if meta.request_model is not None and method_to_action(method) == "read":
        parameters = parameters + _query_parameters(meta.request_model, schemas)
    if meta.query_model is not None:
        parameters = parameters + _query_parameters(meta.query_model, schemas)
    if parameters:
        _check_unique_parameters(parameters, path=meta.path, method=method)
    return parameters


def _operation(meta: RouteMetadata, method: str, schemas: _ComponentSchemas) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "operationId": _operation_id(method, meta.path),
        "summary": meta.summary,
        "tags": list(meta.tags),
        "responses": _operation_responses(meta, method, schemas),
    }
    if meta.description:
        operation["description"] = meta.description

    parameters = _operation_parameters(meta, method, schemas)
    if parameters:
        operation["parameters"] = parameters

    # A body-reading (write) method takes a JSON ``requestBody`` from its
    # ``request_model`` in validation mode; a read method documents the model as query
    # params instead.
    if meta.request_model is not None and method_to_action(method) == "write":
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": schemas.ref(meta.request_model, _VALIDATION)}},
        }

    if meta.authed:
        operation["security"] = [{_SECURITY_SCHEME: []}]

    # A destructive route (an operation flagged ``destructive`` or a DELETE the
    # adapter auto-forced) advertises it so a client can gate the call.
    if meta.destructive:
        operation["x-destructive"] = True

    return operation


def _ref_targets(node: Any) -> Iterator[str]:
    """Every component name a ``#/components/schemas/<name>`` reference inside ``node`` points at.

    Walks nested mappings and lists, yielding the bare name after the prefix. Two forms carry
    such a reference in OpenAPI 3.1: a ``$ref`` string, and each value of a
    ``discriminator.mapping`` object (a mapping value may also be a bare schema name, which
    is not a ``#/components/schemas/`` reference and is skipped). A reference to anything else
    (a header, an external target) is skipped.
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith(_REF_PREFIX):
                yield value[len(_REF_PREFIX) :]
            elif key == "discriminator" and isinstance(value, Mapping):
                mapping = value.get("mapping")
                if isinstance(mapping, Mapping):
                    for target in mapping.values():
                        if isinstance(target, str) and target.startswith(_REF_PREFIX):
                            yield target[len(_REF_PREFIX) :]
                yield from _ref_targets(value)
            else:
                yield from _ref_targets(value)
    elif isinstance(node, list):
        for item in node:
            yield from _ref_targets(item)


def _referenced_components(paths: Mapping[str, Any], components: Mapping[str, Any]) -> set[str]:
    """The component names the document actually uses.

    Seeds from every ``$ref`` in ``paths`` plus the reserved response-envelope names the
    emitter builds and references itself, then follows ``$ref``s transitively through the
    definitions in ``components`` so a nested schema a kept schema references stays too. The
    walk drains its frontier in sorted order for a deterministic traversal; the result is a
    set, so it is order-independent regardless.

    A query model is inlined as parameters by :func:`_query_parameters` and its own top-level
    schema is never ``$ref``'d, so it falls out of this set — unless something else references
    it, in which case it legitimately stays.
    """
    reachable: set[str] = set()
    frontier = sorted(set(_ref_targets(paths)) | _RESERVED_SCHEMA_NAMES)
    while frontier:
        name = frontier.pop()
        if name in reachable:
            continue
        reachable.add(name)
        schema = components.get(name)
        if schema is None:
            continue
        frontier.extend(sorted(target for target in _ref_targets(schema) if target not in reachable))
    return reachable


def build_openapi_spec() -> dict[str, Any]:
    """Build the OpenAPI 3.1 document for the ``/api/*`` surface.

    Offline: reads the route-metadata registry only. Every registered ``/api/*``
    route appears; reload-gated routes carry the retriable ``503`` response.
    """
    components: dict[str, Any] = {
        _ERROR_SCHEMA: {
            "type": "object",
            "properties": {
                "error": {"type": "string"},
                # ``code`` is the machine-readable reason a raiser opts into via
                # ``extra={"code": …}`` (e.g. the 501 not-configured family), merged into
                # the body beside ``error`` by the route adapter. Optional — absent from
                # ``required`` — so an error carrying only ``error`` still validates.
                "code": {
                    "type": "string",
                    "description": (
                        "Stable machine-readable reason a client keys a dedicated error state on, "
                        "present on refusals that opt in (e.g. a 501 not-configured refusal). "
                        "Optional: absent when the error carries only a human-readable message."
                    ),
                },
            },
            "required": ["error"],
        },
        _RELOADING_ERROR_SCHEMA: {
            "type": "object",
            "properties": {
                "error": {"type": "string", "const": REJECT_MESSAGE},
                "reloading": {"type": "boolean", "const": True},
            },
            "required": ["error", "reloading"],
        },
    }

    metas = list(load_api_routes())
    schemas = _component_schemas(_route_model_modes(metas), components)

    paths: dict[str, dict[str, Any]] = {}
    for meta in metas:
        oapath = paths.setdefault(_openapi_path(meta.path), {})
        for method in meta.methods:
            oapath[method.lower()] = _operation(meta, method, schemas)

    # Publish only the components the document references. Every ``(model, mode)`` pair was
    # generated so ``schemas`` can resolve a query model's validation schema for
    # :func:`_query_parameters`, but a query model is inlined as parameters and never
    # ``$ref``'d, so its own top-level definition must not leak into the spec. Prune to the
    # set reachable from ``paths`` (plus the reserved envelopes); the resolver keeps every
    # definition internally, untouched.
    reachable = _referenced_components(paths, components)
    published = {name: schema for name, schema in components.items() if name in reachable}

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "tai42-skeleton API",
            "version": version("tai42-skeleton"),
            "description": "The operator HTTP surface served under /api/*.",
        },
        "paths": paths,
        "components": {
            "schemas": published,
            "securitySchemes": {_SECURITY_SCHEME: {"type": "apiKey", "in": "header", "name": _API_KEY_HEADER}},
        },
    }
