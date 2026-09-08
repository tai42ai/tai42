"""One canonical request-path form for every access-control path decision.

Every path check — the ``/api``/``/mcp`` exclusion, the reserved-set membership
test, and the route-table match — runs on ONE canonical string, so two checks can
never disagree on the shape of a path (the bypass class this closes), and that form
is the SAME one the router matches. The prefix helper is SEGMENT-aware: ``/api``
guards ``/api`` and ``/api/...`` but never ``/apiary``.

The canonical form is the RAW request path split on ``/``, each segment
percent-decoded exactly ONCE — the single decode the ASGI router applies to an ordinary
segment and :class:`~tai42_skeleton.app.raw_path_route.RawPathRoute` applies to a
record ``{key}`` — then re-encoded for only two bytes: ``/`` → ``%2F`` and ``%`` →
``%25``. The re-encode is reversible, so a data slash inside a segment stays inside ONE
segment (never a separator) and ``%252F`` (a double-encoded slash — a thread key whose
principal itself carries ``/``) stays distinguishable from ``%2F`` (a single encoded
slash). The result is idempotent: canonicalizing a canonical path returns it unchanged,
so a layer may re-canonicalize freely.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

# The one escape a canonical segment may carry that a plain Starlette route does NOT
# reproduce: Starlette matches on the once-decoded ``scope["path"]``, where ``%2F`` has
# become ``/`` and split the segment. Only a raw-path-matched route keeps such a key to
# one segment, so a canonical path carrying this escape may resolve to nothing else.
ENCODED_SLASH = "%2F"


class MalformedPathError(ValueError):
    """A request path that cannot be reasoned about safely: after its single decode a
    segment carries a NUL byte, an ASCII control char, or a backslash, or the raw target
    is not ASCII. The classifier treats it as fail-closed (never the SPA shell) and logs
    it; settings validation treats it as a config error."""


def canonicalize_path(path: str) -> str:
    """The single canonical form of ``path``.

    ``path`` is the RAW (undecoded) request target, a registered route template, or an
    already-canonical path (this function is idempotent). Each ``/``-separated segment is
    percent-decoded ONCE — the same single decode the ASGI router / ``RawPathRoute``
    applies — then re-encoded for only ``/`` → ``%2F`` and ``%`` → ``%25`` so a data
    slash never reads as a separator and a double-encoded slash stays distinct from a
    single one.

    1. Decode each segment once. A decoded NUL, ASCII control char (``\\x00``-``\\x1f``,
       ``\\x7f``), or backslash raises :class:`MalformedPathError` rather than being
       coerced into something servable.
    2. Collapse duplicate slashes and resolve ``.``/``..`` per DECODED segment, never
       letting a ``..`` walk above root (an escaping ``..`` normalizes to root).
    3. Re-encode each surviving segment reversibly (``%`` first, then ``/``).

    The result is an absolute, slash-collapsed, dot-resolved path with no trailing
    slash (except root ``/``). Case is preserved — the API is mounted lowercase, so
    case-sensitivity is intentional.
    """
    segments: list[str] = []
    for raw_segment in path.split("/"):
        decoded = unquote(raw_segment)
        for ch in decoded:
            code = ord(ch)
            if ch == "\\" or code < 0x20 or code == 0x7F:
                raise MalformedPathError(f"path {path!r} contains a NUL, control, or backslash byte")
        if decoded in ("", "."):
            # "" is a duplicate/leading/trailing slash; "." is the current dir.
            continue
        if decoded == "..":
            if segments:
                segments.pop()
            # A ".." with nothing to pop escapes root — drop it (normalize to root).
            continue
        segments.append(decoded.replace("%", "%25").replace("/", "%2F"))
    return "/" + "/".join(segments)


def request_canonical_path(scope: Mapping[str, Any]) -> str:
    """The canonical form of the CURRENT request's path, taken from the RAW target
    (``scope["raw_path"]``, bytes/ASCII) with any mounted ``root_path`` stripped, so a
    percent-encoded slash inside a segment (a state record ``{key}``) is reasoned about as
    ONE segment — the SAME form the router matches. Falls back to the once-decoded
    ``scope["path"]`` only when the ASGI server supplies no ``raw_path`` (no encoded slash
    can then be present to lose). A non-ASCII raw target raises
    :class:`MalformedPathError` (fail-closed)."""
    raw = scope.get("raw_path")
    root_path = scope.get("root_path", "")
    if raw is None:
        return canonicalize_path(strip_root_path(scope["path"], root_path))
    try:
        target = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MalformedPathError("raw request path is not ASCII") from exc
    return canonicalize_path(strip_root_path(target, root_path))


def strip_root_path(path: str, root_path: str) -> str:
    """``path`` with a mounted ``root_path`` prefix removed, exactly as Starlette's
    ``get_route_path`` strips it before routing. ``root_path`` is a mount literal without
    percent-encoding, so the plain prefix strip keeps the remainder — an encoded slash
    inside a parameter included — intact."""
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


def under_prefix(path: str, prefix: str) -> bool:
    """Whether ``path`` is ``prefix`` itself or a path-segment descendant of it —
    ``path == prefix or path.startswith(prefix + "/")``. Segment-aware, never a bare
    ``startswith`` (which would leak ``/apiary`` past a ``/api`` guard)."""
    return path == prefix or path.startswith(f"{prefix}/")
