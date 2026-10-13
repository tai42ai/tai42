"""The MCP endpoint a request path addresses — the endpoint a client configures as its server URL.

Derived from the live route registry's mounted transport records, read by record name, and
from path SHAPE under the sub-MCP mount — never from a path literal: a deployment that
serves the streamable-HTTP transport at a configured path is covered by construction. The
sub-MCP rule never asks whether a slug exists, so every well-formed slug gets the same
answer on every worker and replica, and the answer is no slug oracle.
"""

from __future__ import annotations

from dataclasses import dataclass

from tai42_skeleton.access_control.path_canon import canonicalize_path, under_prefix
from tai42_skeleton.app.route_registry import route_registry
from tai42_skeleton.app.sub_mcp_app import SSE_MESSAGES_SEGMENT, SSE_STREAM_SEGMENT, is_valid_slug

# The record names the transport surfaces are recorded under (``serving_core.py``, ``http.py``).
STREAMABLE_HTTP_RECORD = "mcp_streamable_http"
SSE_STREAM_RECORD = "mcp_sse_stream"
SSE_MESSAGES_PREFIX_RECORD = "mcp_sse_messages_prefix"
SSE_MESSAGES_RECORD = "mcp_sse_messages"
SUB_MCP_MOUNT_RECORD = "sub_mcp_mount"

_TEMPLATE_TAIL = "/{path:path}"

# The SSE stream path each message prefix pairs with, both canonical: the two surfaces one
# ``record_sse_surface`` call records. The registry records them independently.
_sse_stream_by_message_prefix: dict[str, str] = {}


def record_sse_pairing(stream_path: str, message_prefix: str) -> None:
    """Record that the SSE message endpoint at ``message_prefix`` belongs to the stream at ``stream_path``.

    Replaces an earlier pairing of the same prefix, as the registry replaces a path's mounted records.
    """
    _sse_stream_by_message_prefix[canonicalize_path(message_prefix)] = canonicalize_path(stream_path)


@dataclass(frozen=True)
class _EndpointTable:
    streamable: frozenset[str]
    sse_streams: frozenset[str]
    # (message prefix, its stream path)
    sse_messages: tuple[tuple[str, str], ...]
    sub_mcp_prefixes: tuple[str, ...]


_table: tuple[int, _EndpointTable] | None = None


def _mount_prefix(template: str) -> str:
    if not template.endswith(_TEMPLATE_TAIL):
        raise RuntimeError(f"mounted record {template!r} is not a '<prefix>{_TEMPLATE_TAIL}' template")
    return canonicalize_path(template[: -len(_TEMPLATE_TAIL)])


def _build_table() -> _EndpointTable:
    streamable: set[str] = set()
    sse_streams: set[str] = set()
    message_prefixes: set[str] = set()
    sub_mcp_prefixes: list[str] = []
    for meta in route_registry.routes():
        if not meta.mounted:
            continue
        if meta.name == STREAMABLE_HTTP_RECORD:
            streamable.add(canonicalize_path(meta.path))
        elif meta.name == SSE_STREAM_RECORD:
            sse_streams.add(canonicalize_path(meta.path))
        elif meta.name == SSE_MESSAGES_PREFIX_RECORD:
            message_prefixes.add(canonicalize_path(meta.path))
        elif meta.name == SSE_MESSAGES_RECORD:
            message_prefixes.add(_mount_prefix(meta.path))
        elif meta.name == SUB_MCP_MOUNT_RECORD:
            sub_mcp_prefixes.append(_mount_prefix(meta.path))
    sse_messages: list[tuple[str, str]] = []
    for prefix in sorted(message_prefixes):
        stream = _sse_stream_by_message_prefix.get(prefix)
        if stream is None:
            raise RuntimeError(f"the SSE message endpoint {prefix!r} is recorded without the stream it belongs to")
        sse_messages.append((prefix, stream))
    return _EndpointTable(
        streamable=frozenset(streamable),
        sse_streams=frozenset(sse_streams),
        sse_messages=tuple(sse_messages),
        sub_mcp_prefixes=tuple(sub_mcp_prefixes),
    )


def _endpoint_table() -> _EndpointTable:
    """The endpoint table derived from the registry, rebuilt whenever the registry records anything."""
    global _table
    version = route_registry.version
    if _table is None or _table[0] != version:
        _table = (version, _build_table())
    return _table[1]


def mcp_endpoint_for(path: str) -> str | None:
    """The path of the MCP endpoint that the app-relative CANONICAL request ``path`` addresses, or ``None``.

    - the streamable-HTTP endpoint ``P``: ``P`` itself;
    - the SSE stream ``S``: itself; its message endpoint ``M`` and everything beneath it: ``S``
      (a client posts its messages to ``M`` with a query);
    - under the sub-MCP mount ``X``: ``X/<slug>`` and ``X/<slug>/sse`` are themselves,
      ``X/<slug>/messages`` and everything beneath it map to ``X/<slug>/sse``; any other path
      under the mount, or a malformed slug, is ``None``.
    """
    table = _endpoint_table()
    if path in table.streamable or path in table.sse_streams:
        return path
    for prefix, stream in table.sse_messages:
        if under_prefix(path, prefix):
            return stream
    for mount in table.sub_mcp_prefixes:
        if under_prefix(path, mount) and path != mount:
            return _sub_mcp_endpoint(mount, path[len(mount) + 1 :].split("/"))
    return None


def _sub_mcp_endpoint(mount: str, segments: list[str]) -> str | None:
    slug = segments[0]
    if not is_valid_slug(slug):
        return None
    endpoint = f"{mount}/{slug}"
    if len(segments) == 1:
        return endpoint
    if segments[1] == SSE_STREAM_SEGMENT and len(segments) == 2:
        return f"{endpoint}/{SSE_STREAM_SEGMENT}"
    if segments[1] == SSE_MESSAGES_SEGMENT:
        return f"{endpoint}/{SSE_STREAM_SEGMENT}"
    return None
