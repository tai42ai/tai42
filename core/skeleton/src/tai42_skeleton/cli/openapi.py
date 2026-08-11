"""The OpenAPI 3.1 emitter — turns the route-metadata registry into a spec.

The app is a FastMCP + Starlette ``custom_route`` server, so it emits no schema
of its own. This module walks the shared route-enumeration primitive
(:func:`tai42_skeleton.app.route_registry.load_api_routes`) and builds a valid
OpenAPI 3.1 document: one operation per method, api-key ``security`` for authed
routes, request bodies from ``request_model.model_json_schema()``, and responses
that wrap the ``{"data": ...}`` success envelope and the ``{"error": ...}``
failure envelope.

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
from importlib.metadata import version
from typing import Any

from pydantic import BaseModel

from tai42_skeleton.app.reload_gate import REJECT_MESSAGE
from tai42_skeleton.app.route_registry import RouteMetadata, load_api_routes, method_to_action

_SECURITY_SCHEME = "ApiKeyAuth"
_API_KEY_HEADER = "x-api-key"

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
    """Rewrite Starlette path params to OpenAPI form, dropping the ``:path``
    converter suffix (``/x/{p:path}`` -> ``/x/{p}``)."""
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
    """Write ``schema`` under ``name`` in ``components``, raising LOUDLY on a name
    collision that would otherwise silently keep or overwrite the wrong schema — a
    reserved envelope name, or two distinct models (or ``$defs``) sharing a
    ``__name__`` with differing schemas. Re-registering an identical schema (the
    same model reached twice) is a no-op."""
    if name in _RESERVED_SCHEMA_NAMES:
        raise ValueError(f"schema name {name!r} collides with a reserved response-envelope component")
    existing = components.get(name)
    if existing is not None and existing != schema:
        raise ValueError(f"schema name {name!r} maps to two distinct schemas — a component-name collision")
    components[name] = schema


def _register_model(model: type[BaseModel], components: dict[str, Any]) -> str:
    """Merge ``model``'s JSON schema (and its ``$defs``) into ``components`` and
    return the component name to ``$ref``."""
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    for def_name, def_schema in schema.pop("$defs", {}).items():
        _assign_component(components, def_name, def_schema)
    _assign_component(components, model.__name__, schema)
    return model.__name__


_NULL_BRANCH = {"type": "null"}


def _query_schema(prop_schema: dict[str, Any]) -> dict[str, Any]:
    """A field's JSON schema as a QUERY parameter's schema, with nullability stripped.

    A query string carries no JSON ``null``: a client omits a parameter, it never sends one
    valued null. So pydantic's rendering of ``T | None`` — ``anyOf [T, null]`` plus
    ``default: null`` — is collapsed to plain ``T`` with no default, and a union of several
    real branches keeps its ``anyOf`` minus the null branch. Sibling keywords (``title``,
    ``description``, bounds) survive the collapse. A non-nullable schema passes through."""
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


def _query_parameters(model: type[BaseModel], components: dict[str, Any]) -> list[dict[str, Any]]:
    """A model's fields as ``in: query`` parameters — a read method's ``request_model``,
    or any method's ``query_model``.

    A model whose fields are query inputs (a GET reading its inputs from the query string,
    or a door declaring a ``query_model``) turns each field into a query parameter, never a
    request body. Each parameter carries the field's own JSON schema — a ``list[…]`` field
    keeps its ``array`` schema, published as a repeated ``?name=`` param under OpenAPI's
    default form serialization — stripped of nullability by :func:`_query_schema`, and is
    ``required`` exactly when the model marks it required (a field with a default is
    optional). The field's description rides on the PARAMETER, not on its schema: it is the
    Parameter Object's own ``description`` that a generator renders, so it is moved there
    rather than left where only a schema-aware reader would find it. Any ``$defs`` a field
    schema references are merged into ``components`` so the ``$ref``s resolve."""
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    for def_name, def_schema in schema.pop("$defs", {}).items():
        _assign_component(components, def_name, def_schema)
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
    fails the emission LOUDLY naming the route and the parameter."""
    seen: set[tuple[str, str]] = set()
    for parameter in parameters:
        key = (parameter["name"], parameter["in"])
        if key in seen:
            raise ValueError(
                f"{method} {path} declares parameter {key[0]!r} in {key[1]} twice — "
                "the path, the request_model, and the query_model must not claim the same name"
            )
        seen.add(key)


def _json_envelope_schema(meta: RouteMetadata, components: dict[str, Any]) -> dict[str, Any]:
    if meta.response_model is None:
        data_schema: dict[str, Any] = {}
    else:
        data_schema = {"$ref": f"#/components/schemas/{_register_model(meta.response_model, components)}"}
    return {
        "type": "object",
        "properties": {"data": data_schema},
        "required": ["data"],
    }


def _success_response(meta: RouteMetadata, method: str, components: dict[str, Any]) -> dict[str, Any]:
    """The 200/2xx response for ``method``, documenting every content type it serves.
    ``application/json`` carries the ``{"data": ...}`` envelope; a streaming, CSV,
    HTML, or asset/download type answers its own media type instead. A method that
    serves more than one type (the runs export: CSV or a JSON download) lists them
    all under ``content``."""
    media_types = meta.success_media_types[method]
    content: dict[str, Any] = {}
    for media_type in media_types:
        if media_type == "application/json":
            content[media_type] = {"schema": _json_envelope_schema(meta, components)}
        else:
            content[media_type] = {"schema": {"type": "string"}}
    if len(media_types) == 1 and media_types[0] != "application/json":
        description = _NON_JSON_DESCRIPTIONS.get(media_types[0], "Success.")
    else:
        description = "Success."
    return {"description": description, "content": content}


def _error_response(status: int) -> dict[str, Any]:
    """The response for a status the route answers with the plain ``{"error": ...}``
    envelope — every entry of ``error_statuses``, the declared ``503`` included."""
    return {
        "description": _STATUS_DESCRIPTIONS.get(status, "Error."),
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{_ERROR_SCHEMA}"}}},
    }


def _reload_gate_response(*, also_unavailable: bool) -> dict[str, Any]:
    """The reload gate's ``503``: the constant-message ``ReloadingError`` body plus the
    ``Retry-After`` header the gate stamps.

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


def _operation(meta: RouteMetadata, method: str, components: dict[str, Any]) -> dict[str, Any]:
    responses: dict[str, Any] = {str(meta.success_status): _success_response(meta, method, components)}
    for status in meta.additional_success_statuses:
        responses[str(status)] = _success_response(meta, method, components)
    for status in meta.error_statuses:
        responses[str(status)] = _error_response(status)
    # The reload gate is declared as ``reload_gated``, never as an error status, so its
    # response is added here and OVERWRITES a declared 503 — merged into one slot that
    # admits both bodies when the route answers both.
    if meta.reload_gated:
        responses["503"] = _reload_gate_response(also_unavailable=503 in meta.error_statuses)

    operation: dict[str, Any] = {
        "operationId": _operation_id(method, meta.path),
        "summary": meta.summary,
        "tags": list(meta.tags),
        "responses": responses,
    }
    if meta.description:
        operation["description"] = meta.description

    # A route's typed ``request_model`` is documented according to how the method carries
    # it: a body-reading (write) method takes a JSON ``requestBody``; a read method
    # (GET/HEAD) reads its inputs from the query string, so the model's fields become
    # ``in: query`` parameters instead (a GET request body would misdocument the
    # endpoint). A ``query_model`` is additive to either: its fields are ``in: query`` for
    # ANY method, so a WRITE-method door publishes the query it reads at the edge. Path
    # parameters always precede the model-derived ones.
    parameters = _path_parameters(meta.path)
    if meta.request_model is not None and method_to_action(method) == "read":
        parameters = parameters + _query_parameters(meta.request_model, components)
    if meta.query_model is not None:
        parameters = parameters + _query_parameters(meta.query_model, components)
    if parameters:
        _check_unique_parameters(parameters, path=meta.path, method=method)
        operation["parameters"] = parameters

    if meta.request_model is not None and method_to_action(method) == "write":
        ref = _register_model(meta.request_model, components)
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ref}"}}},
        }

    if meta.authed:
        operation["security"] = [{_SECURITY_SCHEME: []}]

    # A destructive route (an operation flagged ``destructive`` or a DELETE the
    # adapter auto-forced) advertises it so a client can gate the call; a
    # non-destructive route emits nothing.
    if meta.destructive:
        operation["x-destructive"] = True

    return operation


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

    paths: dict[str, dict[str, Any]] = {}
    for meta in load_api_routes():
        oapath = paths.setdefault(_openapi_path(meta.path), {})
        for method in meta.methods:
            oapath[method.lower()] = _operation(meta, method, components)

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "tai42-skeleton API",
            "version": version("tai42-skeleton"),
            "description": "The operator HTTP surface served under /api/*.",
        },
        "paths": paths,
        "components": {
            "schemas": components,
            "securitySchemes": {_SECURITY_SCHEME: {"type": "apiKey", "in": "header", "name": _API_KEY_HEADER}},
        },
    }
