"""The HTTP route-registration surface: the route action, its declared metadata, and ``AppHttp``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

#: A route's authorization character — the single class deciding how a route is
#: reached. ``read`` / ``write`` are grantable through a role's per-tag level;
#: ``fenced`` is the admin-only mutation fence and ``secret`` the admin-only
#: bulk-secret read fence, neither opened by any per-tag level. Passed to
#: :meth:`AppHttp.custom_route` as ``action`` (``None`` lets the surface derive
#: the grantable class from the HTTP methods).
RouteAction = Literal["read", "write", "fenced", "secret"]


@dataclass(frozen=True)
class DeclaredRouteMetadata:
    """The behavioral OpenAPI properties a route DECLARES.

    A route registered through the operations adapter supplies this
    from its operation's metadata + declared error classes; a native ``/api/*``
    handler passes it explicitly at its ``custom_route`` registration. Its
    ``reload_gated`` / ``reads_body`` / error statuses / success status feed the
    emitted spec and the coverage/parity gates.

    ``additional_success_statuses`` names further 2xx codes one method may answer
    besides ``success_status``; each is emitted as its own success response.
    """

    reload_gated: bool
    reads_body: bool
    error_statuses: tuple[int, ...]
    success_status: int
    additional_success_statuses: tuple[int, ...] = ()


@runtime_checkable
class AppHttp(Protocol):
    def middleware(self, cls: type[Any] | None = None, **options: Any) -> Callable[..., Any]: ...

    def custom_route(
        self,
        path: str,
        methods: list[str],
        name: str | None = None,
        include_in_schema: bool = True,
        *,
        summary: str,
        tags: list[str],
        response_model: type[BaseModel] | None,
        request_model: type[BaseModel] | None = None,
        query_model: type[BaseModel] | None = None,
        authed: bool | None = None,
        destructive: bool = False,
        action: RouteAction | None = None,
        declared: DeclaredRouteMetadata | None = None,
        no_body_reason: str | None = None,
        enveloped: bool = True,
    ) -> Callable[[Callable[[Request], Awaitable[Response]]], Callable[[Request], Awaitable[Response]]]:
        """Register a Starlette handler AND its self-describing route metadata.

        The metadata is the single source of truth the OpenAPI 3.1 emitter and its
        coverage gate consume, so every registration MUST describe itself:

        * ``summary`` — a one-line operation summary (required, non-empty).
        * ``tags`` — at least one OpenAPI tag grouping the route (required).
        * ``response_model`` — the pydantic model (any ``BaseModel``, ``RootModel``
          included) describing the success body. Required — there is no default, so a
          route cannot silently omit it. By default the model is wrapped in the
          ``{"data": ...}`` success envelope; a route that answers a RAW top-level body
          passes ``enveloped=False`` (see below). ``None`` is legal ONLY together with a
          non-blank ``no_body_reason``: the two states are mutually exclusive — a route
          declares a typed body OR a reasoned no-body, never both and never neither.
        * ``enveloped`` — whether the JSON success body is wrapped in the
          ``{"data": <response_model>}`` envelope (the default, ``True``) or is the
          model's schema DIRECTLY at the top level (``False``, a raw non-enveloped
          body). ``enveloped=False`` REQUIRES a ``response_model``: an unwrapped body
          still needs a real model, so a bare ``None`` under it is a registration error.
        * ``no_body_reason`` — the required-when-``response_model`` is ``None``
          justification for a route that serves no ``{"data": <model>}`` JSON body
          (a redirect, a raw/streaming/asset response, a vendor webhook 200, or a
          raw non-enveloped body the envelope emitter cannot describe). Passing it
          alongside a ``response_model``, or omitting it when ``response_model`` is
          ``None``, is a registration error.
        * ``request_model`` — the pydantic model of the request body; required for
          any route that reads a body, omitted (``None``) otherwise.
        * ``query_model`` — the pydantic model whose fields are published as ``in:
          query`` parameters for ANY method, additive to ``request_model``. A read
          door's ``request_model`` already emits as query, so this is the only way a
          WRITE-method door documents the query it reads at the edge; ``None`` (the
          default) publishes no query model.
        * ``authed`` — whether the route requires the api key; emitted as the
          ``security`` requirement. ``None`` (the default) defers to the runtime:
          a core route resolves to ``True``, a declared plugin route to the
          negation of its ``tai-plugin.yml`` ``public`` flag. Passing an explicit
          bool from a declared plugin route module is a registration error — the
          declaration is the single source of that decision.
        * ``destructive`` — whether the route mutates in a way flagged as
          destructive (default ``False``); emitted as ``x-destructive``.
        * ``action`` — the route's authorization character (a :data:`RouteAction`);
          ``None`` (the default) lets the surface derive the grantable class from
          the HTTP methods.
        * ``declared`` — the route's behavioral OpenAPI properties (a
          :class:`DeclaredRouteMetadata`): its ``reload_gated`` / ``reads_body`` /
          error statuses / success status. A route in the ``/api/*`` spec surface
          declares these (the operations adapter passes its operation's metadata, a
          native handler passes its own); ``None`` (the default) records trivial
          defaults for a route outside the spec surface, whose behavioral metadata
          is never emitted.

        The handler's narrative docstring becomes the operation description, and
        the reload-gate ``503`` response is derived from the handler body."""
        ...

    def use_raw_path_key(self, path_prefix: str) -> None:
        """Match every already-registered route whose template starts with ``path_prefix``
        against the raw (undecoded) request path, so a path parameter whose values carry
        ``/`` (a state record's ``{key}``, addressed as one percent-encoded segment) stays
        ONE segment instead of splitting once the ASGI server has decoded ``%2F``; the
        matched parameters are decoded once after the match. Called AFTER the routes are
        registered, over their shared prefix, so it covers a family of doors at one seam. A
        no-op where no route table is served (the offline spec harness)."""
        ...

    def mount_base(self) -> str:
        """The resolved absolute mount base of the declared plugin route module
        importing now — ``/api/`` + the item's mount base, no trailing slash
        (e.g. ``/api/channels/telegram``).

        Callable ONLY while a declared plugin route module imports — the mount
        binding is present then. A module captures this value at import for later
        use (a startup hook building an external webhook URL, a login descriptor's
        self-referential path) so a remapped base is followed instead of a
        hardcoded default. Called with no binding present — a core or
        operator-authored module — it raises: those modules own no declared mount."""
        ...
