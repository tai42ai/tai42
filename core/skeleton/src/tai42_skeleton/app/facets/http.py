"""The ``app.http`` facade (middleware/custom_route/mount-base)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import _Facet

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from pydantic import BaseModel
    from starlette.requests import Request
    from starlette.responses import Response
    from tai42_contract.app import DeclaredRouteMetadata

    from tai42_skeleton.app.route_registry import RouteAction


class HttpFacet(_Facet):
    """``app.http`` — middleware + custom-route registration (``AppHttp``)."""

    def middleware(self, cls: type | None = None, **options: Any) -> Callable[..., Any]:
        return self._app._http_surface.middleware(cls, **options)

    def mount_base(self) -> str:
        return self._app._http_surface.mount_base()

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
        return self._app._http_surface.custom_route(
            path,
            methods,
            name,
            include_in_schema,
            summary=summary,
            tags=tags,
            response_model=response_model,
            request_model=request_model,
            query_model=query_model,
            authed=authed,
            destructive=destructive,
            action=action,
            declared=declared,
            no_body_reason=no_body_reason,
            enveloped=enveloped,
        )

    def use_raw_path_key(self, path_prefix: str) -> None:
        return self._app._http_surface.use_raw_path_key(path_prefix)
