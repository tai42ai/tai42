"""The route-adapter helper — turns an operation into a thin HTTP route.

An adapter route is declarative: this helper registers the HTTP surface over
:meth:`HttpSurface.custom_route`, and the generated handler parses/validates the
flat typed parameters, calls the operation, wraps the success payload in the
``{"data": ...}`` envelope, and maps the operation's declared errors to statuses.
Its reload gate is declared from the operation's metadata and enforced by the
route surface. It NEVER performs
authorization — that is a separate consumer (the HTTP middleware at the route
edge, ``AuthzMiddleware`` at the tool edge).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp.utilities.types import Audio, File, Image
from pydantic import BaseModel, ValidationError
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Message
from tai42_contract.errors import OperationError

from tai42_skeleton.app.route_registry import DeclaredRouteMetadata, RouteAction
from tai42_skeleton.operations.errors import BadRequestError

if TYPE_CHECKING:
    from tai42_skeleton.operations.registry import OperationMetadata

# Methods whose adapter reads a JSON request body; a GET reads its typed
# parameters from the query string instead (never a GET body).
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# A request-edge extractor: derives an operation's flat kwargs from the raw
# request (header credentials, or a body the operation validates itself). It may
# raise an :class:`OperationError` to reject a malformed input with its status.
ContextExtractor = Callable[[Request], Awaitable[dict[str, Any]]]

# Path-template segment (``{name}`` / ``{name:path}``) → the argument name.
_PATH_PARAM = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


@dataclass(frozen=True)
class OperationResponse:
    """An operation result that carries a Starlette ``BackgroundTask`` to run after the reply.

    The env-change doors return this to arm the writer's own post-reply graceful self-exit after
    a recycle roll that converged. On the HTTP edge the adapter envelopes ``payload`` and attaches
    ``background`` to the response, so it runs after the body flushes — never an inline
    ``create_task`` that could race the flush and sever the transport before the reply ships.
    The projected tool edge (every tool door: MCP ``tools/call``, ``tai tools run``, an agent, a
    preset, a hook, a schedule or a submit run inside a backend job) runs ``background`` once the
    operation returned and then answers with ``payload``; the exit is the worker's graceful stop,
    whose drain lets the in-flight reply or job finish before the worker stops and its supervisor
    respawns it. One reply, true on every edge.
    """

    payload: Any
    background: BackgroundTask | None = None


def _path_param_names(path: str) -> tuple[str, ...]:
    return tuple(_PATH_PARAM.findall(path))


def validation_error_fields(exc: ValidationError) -> list[dict[str, Any]]:
    """Field paths + error types from a pydantic ``ValidationError``, values stripped.

    A pydantic v2 error entry carries the offending ``input`` (and a ``ctx`` that can
    hold values), and ``str(exc)`` renders them inline — a secret in a rejected field
    (e.g. a connector ``config_values`` value) would otherwise ride the 400/422 body.
    This yields only the dotted field ``loc`` and the error ``type`` per entry, so a
    client learns WHICH field failed and HOW, never WHAT was sent.
    """
    return [{"loc": list(error["loc"]), "type": error["type"]} for error in exc.errors(include_url=False)]


# A body that does not parse: ``json.JSONDecodeError`` (and an undecodable byte sequence) is a
# ``ValueError``; a deeply nested body raises ``RecursionError`` out of the decoder. Any other
# exception is a server bug and propagates.
JSON_PARSE_ERRORS = (ValueError, RecursionError)


def _json_object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise BadRequestError("body must be a JSON object")
    return body


async def read_json_object(request: Request) -> dict[str, Any]:
    """The request body as a JSON object; ``BadRequestError`` for a malformed body or a non-object one.

    The refusal texts carry nothing of the input.
    """
    try:
        body = await request.json()
    except JSON_PARSE_ERRORS as exc:
        raise BadRequestError("invalid JSON body") from exc
    return _json_object(body)


def parse_json_object(raw: bytes) -> dict[str, Any]:
    """``raw`` — body bytes a door already read under its own bound — as a JSON object, by the same rule."""
    try:
        body = json.loads(raw)
    except JSON_PARSE_ERRORS as exc:
        raise BadRequestError("invalid JSON body") from exc
    return _json_object(body)


def validate_body[M: BaseModel](model: type[M], body: object, what: str) -> M:
    """``body`` validated as ``model``; ``BadRequestError("invalid <what>: …")`` naming each violated rule.

    ``body`` is any decoded JSON value: a value that is not an object is refused by the model's own
    rule ("Input should be a valid dictionary or instance of …").

    Value-free: each pydantic error's ``msg`` names the bound it violates, never the input, so the
    refusal never echoes what was sent.
    """
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise BadRequestError(f"invalid {what}: " + "; ".join(error["msg"] for error in exc.errors())) from exc


def request_with_body(request: Request, body: bytes) -> Request:
    """A request over the same ASGI scope whose body stream yields ``body`` once — the bytes a door already read.

    Built through Starlette's public constructor, so a parser reading ``json()``, ``form()`` or
    ``body()`` of it sees exactly the bytes the door bounded; after the one body message the
    stream reports a disconnect.
    """
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(request.scope, receive)


def _declared_metadata(
    op: OperationMetadata,
    method: str,
    *,
    authed: bool,
    success_status: int,
    request_model: type[BaseModel] | None,
) -> DeclaredRouteMetadata:
    """The route's behavioral properties taken from the operation, not divined.

    ``error_statuses`` are the statuses answered with the plain ``{"error": ...}``
    envelope this adapter renders: the operation's own typed errors, plus the ``401``
    the auth edge answers for an authed route. The reload gate's ``503`` is NOT among
    them — it answers a different body (the constant-message reloading envelope with a
    ``Retry-After`` header), and ``reload_gated`` already declares it, so the emitter
    owns that response.

    ``request_model`` is the model published for THIS route, so ``reads_body`` follows
    the shape the route actually documents.
    """
    statuses: set[int] = {cls.status for cls in op.error_classes}
    if authed:
        statuses.add(401)
    reads_body = request_model is not None and method in _BODY_METHODS
    return DeclaredRouteMetadata(
        reload_gated=op.reload_gated,
        reads_body=reads_body,
        error_statuses=tuple(sorted(statuses)),
        success_status=success_status,
    )


class _ParseError(Exception):
    """A request-parse rejection carrying the exact status + error body the handler returns.

    A malformed JSON body (400) or a pydantic validation failure (422). Kept distinct
    from :class:`OperationError` so the handler renders the parse body verbatim while the
    operation's own typed errors keep the ``{"error", **extra}`` shape.
    """

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__()
        self.status = status
        self.body = body


async def _read_operation_kwargs(
    request: Request,
    path_params: tuple[str, ...],
    context_extractor: ContextExtractor | None,
    request_model: type[BaseModel] | None,
    reads_body: bool,
) -> dict[str, Any]:
    """Build the operation's flat kwargs from the HTTP edge.

    The path params, plus EITHER the context-extractor's result OR the parsed
    request-model dict OR nothing.

    An :class:`OperationError` the extractor raises is re-raised for the handler to map to
    its status. A malformed JSON body or a pydantic ``ValidationError`` raises a
    :class:`_ParseError` carrying the status + body the handler returns (a validation
    failure yields field paths + types only, never the rejected input values).
    """
    kwargs: dict[str, Any] = {name: request.path_params[name] for name in path_params}
    if context_extractor is not None:
        # A request-shaped input (header credentials, a body the operation validates
        # itself with its own error classes) is derived here at the HTTP edge and passed
        # to the operation as flat kwargs — the operation stays request-free. The
        # extractor REPLACES the default request-model parse; ``request_model`` is then
        # metadata only (the emitted spec's requestBody). A typed error the extractor
        # raises is re-raised for the handler to map to its status.
        extra = await context_extractor(request)
        kwargs.update(extra)
    elif request_model is not None:
        try:
            raw = await request.json() if reads_body else dict(request.query_params)
        except Exception as exc:
            raise _ParseError(400, {"error": "malformed request body"}) from exc
        try:
            model = request_model.model_validate(raw)
        except ValidationError as exc:
            # Field paths + error types only — never the rejected input values, which a
            # pydantic error entry carries and could leak a secret.
            raise _ParseError(422, {"error": validation_error_fields(exc)}) from exc
        kwargs.update(model.model_dump())
    return kwargs


def _unwrap_operation_result(result: Any) -> tuple[Any, BackgroundTask | None]:
    """Split an operation result into ``(payload, background)``.

    An :class:`OperationResponse` carries a post-flush ``BackgroundTask`` (an
    env-change door's writer self-exit) alongside its payload; a bare payload
    returns ``(result, None)``.
    """
    if isinstance(result, OperationResponse):
        return result.payload, result.background
    return result, None


def _build_handler(
    op: OperationMetadata,
    method: str,
    path_params: tuple[str, ...],
    context_extractor: ContextExtractor | None = None,
    response_headers: dict[str, str] | None = None,
    success_status: int = 200,
    request_model: type[BaseModel] | None = None,
) -> Callable[[Request], Awaitable[Response]]:
    reads_body = method in _BODY_METHODS

    async def handler(request: Request) -> Response:
        # A ``reload_gated`` operation's 503 is the route surface's: ``custom_route`` wraps
        # this handler from the declared metadata.
        try:
            kwargs = await _read_operation_kwargs(request, path_params, context_extractor, request_model, reads_body)
            result = await op.func(**kwargs)
        except OperationError as exc:
            # A typed error from the extractor or the operation — ``exc.extra`` carries
            # any additional error-body fields the operation opted into (e.g. a stable UI
            # ``code``); empty for the common error.
            return JSONResponse({"error": exc.message, **exc.extra}, status_code=exc.status)
        except _ParseError as parse:
            return JSONResponse(parse.body, status_code=parse.status)

        # Unwrap a post-flush ``BackgroundTask`` (the env-change writer's self-exit) from the
        # payload; a bare payload's background stays ``None``. ``response_headers`` ride
        # the SUCCESS response only (a caching directive the route's read carries); an
        # error response is uncached and unadorned. ``success_status`` is the operation's
        # own success code — 200 for the common enveloped read/write, or an
        # accepted-but-detached ``202`` (the background tool-run submit door).
        payload, background = _unwrap_operation_result(result)
        return JSONResponse(
            {"data": _serialize(payload)},
            status_code=success_status,
            headers=response_headers,
            background=background,
        )

    handler.__name__ = op.name
    handler.__doc__ = op.func.__doc__
    return handler


def _serialize(result: object) -> Any:
    # A live fastmcp media object (``Image`` / ``Audio`` / ``File``) is not
    # JSON-native — fastmcp's own media-to-content conversion runs only on the MCP
    # tool edge, which the HTTP envelope bypasses. Convert it to its MCP content
    # model first (the same wire shape the direct tool-run path emits), so an
    # operation returning ``str | MediaBlock`` (``get_resource_by_id``) serves media
    # over HTTP as ``{"type": "image"|"audio"|"resource", ...}`` rather than raising.
    # Every existing operation returns dicts/lists/models, so this branch is a no-op
    # for them.
    if isinstance(result, Image):
        result = result.to_image_content()
    elif isinstance(result, Audio):
        result = result.to_audio_content()
    elif isinstance(result, File):
        result = result.to_resource_content()
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    return result


def register_operation_route(
    app: Any,
    op: OperationMetadata,
    *,
    path: str,
    method: str,
    tags: list[str] | None = None,
    authed: bool = True,
    context_extractor: ContextExtractor | None = None,
    request_model: type[BaseModel] | None = None,
    response_headers: dict[str, str] | None = None,
    success_status: int = 200,
    action: RouteAction | None = None,
    self_service: bool = False,
    any_authenticated: bool = False,
    pre_auth: bool = False,
) -> Callable[[Request], Awaitable[Response]]:
    """Register ``op`` as an HTTP route at ``path``/``method`` and return the handler.

    A DELETE route auto-forces the ``destructive`` spec-surface bool on the operation;
    a GET route that declares ``destructive`` is a registration-time error (a read is
    never destructive). ``action`` is the ORTHOGONAL authorization action-class
    (``read``/``write`` derived from the method, or the admin-only ``fenced``/``secret``
    fence declared explicitly) — it never touches the ``destructive`` bool, so a
    ``secret`` GET never trips the GET-destructive guard. The operation's route template
    + method + path params are attached to its metadata so the tool-edge authorization
    can synthesize the concrete resource path.

    ``context_extractor`` derives the operation's non-path kwargs from the raw
    request when they are not a plain request-model parse (header credentials, or a
    body the operation validates itself with its own error classes); when given it
    REPLACES the default request-model parse and ``request_model`` becomes spec
    metadata only.

    ``request_model`` is the model published (and, absent a ``context_extractor``,
    parsed) for THIS route; ``None`` inherits the operation's own. A GET fetch door and
    a POST render door registered over one operation whose ``request_model`` is the POST
    body override the GET's to publish only its narrower query.

    ``response_headers`` are static headers stamped on the SUCCESS response only (a
    caching directive the route's read carries, e.g. ``cache-control: no-cache``) —
    the enveloping/status stay the adapter's, so the route surface is unchanged
    apart from the declared headers.

    ``success_status`` is the HTTP status the enveloped success answers with;
    it defaults to ``200`` and is set to ``202`` for an accepted-but-detached
    submission (the background tool-run submit door), so that door answers
    ``202`` rather than ``200``.

    ``self_service``, ``any_authenticated`` and ``pre_auth`` declare the route's reach and
    pass through to ``custom_route`` unchanged (see :class:`tai42_contract.app.facets.AppHttp`).
    """
    method_upper = method.upper()
    if method_upper == "GET" and op.destructive:
        raise ValueError(
            f"operation {op.name!r} is registered on GET {path} but declares destructive=True; "
            "a read operation is never destructive."
        )
    if method_upper == "DELETE":
        op.destructive = True

    path_params = _path_param_names(path)
    op.route_template = path
    op.http_method = method_upper
    op.path_params = path_params

    route_request_model = request_model if request_model is not None else op.request_model
    handler = _build_handler(
        op, method_upper, path_params, context_extractor, response_headers, success_status, route_request_model
    )
    route_tags = tags if tags is not None else list(op.tags)
    # Register over the concrete HttpSurface directly: the concrete app exposes it
    # as ``_http_surface``, the offline spec harness as ``.http``; either way the
    # resolved surface carries the ``declared`` / ``destructive`` metadata seam.
    surface = getattr(app, "_http_surface", None) or app.http
    decorator = surface.custom_route(
        path,
        methods=[method_upper],
        name=op.name,
        summary=op.summary,
        tags=route_tags,
        request_model=route_request_model,
        response_model=op.response_model,
        query_model=op.query_model,
        authed=authed,
        destructive=op.destructive,
        action=action,
        self_service=self_service,
        any_authenticated=any_authenticated,
        pre_auth=pre_auth,
        no_body_reason=op.no_body_reason,
        enveloped=op.enveloped,
        declared=_declared_metadata(
            op, method_upper, authed=authed, success_status=success_status, request_model=route_request_model
        ),
    )
    return decorator(handler)
