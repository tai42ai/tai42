"""The ``@operation`` decorator — the single declaration point for an operation.

A decorated function is a plain typed async function; the decorator records its
metadata into the :data:`operation_registry` and stamps the metadata onto the
function object (``__operation__``) so the route adapter can pick it up from the
function alone.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from tai42_skeleton.operations.registry import OperationMetadata, OperationRegistry, operation_registry

if TYPE_CHECKING:
    from tai42_skeleton.operations.errors import OperationError

# The attribute the metadata record is stamped onto the decorated function.
OPERATION_ATTR = "__operation__"

AsyncOp = Callable[..., Awaitable[object]]

# The decorator returns the function UNCHANGED, so it is typed identity-preserving:
# a decorated operation keeps its precise ``(params) -> Awaitable[ReturnType]``
# signature for callers (a direct ``await op(...)`` in an op-level test sees the
# real return type, not the erased ``object`` bound).
_AsyncOpT = TypeVar("_AsyncOpT", bound=AsyncOp)


def operation(
    *,
    name: str | None = None,
    summary: str,
    tags: list[str] | None = None,
    destructive: bool = False,
    reload_gated: bool = False,
    meta_executor: bool = False,
    caller_context: bool = False,
    authority_changing: bool = False,
    errors: list[type[OperationError]] | None = None,
    request_model: type[BaseModel] | None = None,
    response_model: type[BaseModel] | None = None,
    query_model: type[BaseModel] | None = None,
    no_body_reason: str | None = None,
    enveloped: bool = True,
    registry: OperationRegistry | None = None,
) -> Callable[[_AsyncOpT], _AsyncOpT]:
    """Declare a function as an operation and register it.

    ``name`` defaults to the function name. ``errors`` names the typed error
    classes the operation may raise (the adapter maps them to statuses; the
    coverage gate asserts each is exercised). ``meta_executor`` marks a
    "run a tool/agent by name" operation — hardcode-blocked from MCP projection
    (tier 1). ``caller_context`` marks an operation whose parameters are the
    caller's OWN identity, injected at the HTTP edge from the authenticated request
    — also hardcode-blocked from projection (tier 1), because as an MCP tool the
    caller would supply those identity params itself and spoof another principal.
    ``authority_changing`` marks an operation that mints/scopes keys, edits policy,
    replaces the manifest, or restores/runs unshipped state — off the default MCP
    surface (tier 2), includable by an explicit ``api_tools``.

    ``request_model`` types the operation's inputs: the emitted spec documents it as
    the JSON ``requestBody`` of a write method, and as the ``in: query`` parameters of
    a read method (which carries no body). It is also what the route adapter parses the
    request with, UNLESS the route supplies a ``context_extractor`` — a door that parses
    its own query/body at the HTTP edge, for which the model is spec metadata only.
    ``query_model`` is always spec metadata only: nothing parses it, it publishes a
    model's fields as ``in: query`` parameters for ANY method, which is how a WRITE door
    documents the query it reads at the edge. Either model's field names — under their
    aliases where they differ — are the WIRE keys a generated client sends, not the
    operation's Python parameter names.

    ``no_body_reason`` declares an operation that serves NO ``{"data": <model>}`` JSON
    body (a streaming/asset/redirect response, or a raw non-enveloped body): it is
    required when ``response_model`` is ``None`` and mutually exclusive with it — the
    route seam raises at registration on a bare ``None`` or on both supplied.

    ``enveloped`` (default ``True``) wraps ``response_model`` in the ``{"data": ...}``
    success envelope; ``enveloped=False`` publishes the model's schema DIRECTLY at the
    top level (a raw non-enveloped body) and REQUIRES a ``response_model`` — the route
    seam raises at registration on ``enveloped=False`` with a bare ``None``.
    """

    target_registry = registry if registry is not None else operation_registry

    def decorator(func: _AsyncOpT) -> _AsyncOpT:
        op_name = name or func.__name__
        metadata = OperationMetadata(
            name=op_name,
            func=func,
            summary=summary,
            tags=tuple(tags or ()),
            destructive=destructive,
            reload_gated=reload_gated,
            meta_executor=meta_executor,
            caller_context=caller_context,
            authority_changing=authority_changing,
            error_classes=tuple(errors or ()),
            request_model=request_model,
            response_model=response_model,
            query_model=query_model,
            no_body_reason=no_body_reason,
            enveloped=enveloped,
        )
        target_registry.register(metadata)
        setattr(func, OPERATION_ATTR, metadata)
        return func

    return decorator


def operation_metadata_of(func: object) -> OperationMetadata:
    """The metadata stamped onto a decorated operation function, or raise."""
    metadata = getattr(func, OPERATION_ATTR, None)
    if not isinstance(metadata, OperationMetadata):
        raise TypeError(f"{func!r} is not an @operation-decorated function")
    return metadata
