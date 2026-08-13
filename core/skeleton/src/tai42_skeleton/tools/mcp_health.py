"""Passive per-MCP dispatch health, keyed by config title.

A process-wide record of each MCP's most recent tool-dispatch outcome. The
tool-call seam records each dispatch's connection outcome; auth-blocked
dispatches record nothing — they signal credential state, not connection
health. The MCP-status op reads the snapshot. State is per-process by construction — each worker records
only the calls it served, and the status op's per-worker report is what
aggregates across the fleet.

The store is a plain module-level dict guarded by one lock: every record is a
short read-modify-write with no ``await`` inside, so a single lock keeps a
concurrent record from a different worker thread or event loop atomic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

# The scheme quantifier is bounded so an adversarial non-URL run of scheme-valid
# chars not followed by ``://`` scans linearly instead of backtracking quadratically.
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]{0,62}://[^\s\"'<>]+")


def _redact(text: str) -> str:
    """Redact URL-embedded credentials in arbitrary text: for each URL, userinfo and
    every query-string VALUE become ``<redacted>``; non-URL text is left untouched."""

    def _one(match: re.Match[str]) -> str:
        # Userinfo runs to the LAST ``@`` in the authority so an inner ``@`` in a
        # password cannot leak.
        url = re.sub(r"(://)[^/?#\s]+@", r"\1<redacted>@", match.group(0))
        base, sep, tail = url.partition("?")
        if not sep:
            return url
        query, hsep, frag = tail.partition("#")
        pairs = []
        for pair in query.split("&"):
            key, eq, _value = pair.partition("=")
            pairs.append(f"{key}{eq}<redacted>" if eq else pair)
        return f"{base}{sep}{'&'.join(pairs)}{hsep}{frag}"

    return _URL_RE.sub(_one, text)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _McpHealth:
    """One MCP's dispatch health. ``consecutive_failures`` is the length of the
    current failure run; ``failing_since`` marks its first error and is cleared
    on the next success alongside ``last_error``."""

    last_success: str | None = None
    last_error: dict[str, str] | None = None
    consecutive_failures: int = 0
    failing_since: str | None = None


_HEALTH: dict[str, _McpHealth] = {}
_HEALTH_LOCK = Lock()


def record_success(title: str) -> None:
    """Record a completed dispatch for ``title`` — ends any current failure run."""
    ts = _now_iso()
    with _HEALTH_LOCK:
        entry = _HEALTH.setdefault(title, _McpHealth())
        entry.last_success = ts
        entry.last_error = None
        entry.consecutive_failures = 0
        entry.failing_since = None


def record_failure(title: str, exc: BaseException) -> None:
    """Record a failed dispatch for ``title`` — extends the current failure run
    and pins ``failing_since`` to the run's first error.

    The stored message never carries URL-embedded credentials — it is a read-tier surface."""
    ts = _now_iso()
    message = _redact(str(exc))
    with _HEALTH_LOCK:
        entry = _HEALTH.setdefault(title, _McpHealth())
        entry.last_error = {"type": type(exc).__name__, "message": message, "at": ts}
        entry.consecutive_failures += 1
        if entry.failing_since is None:
            entry.failing_since = ts


def forget(title: str) -> None:
    """Drop ``title``'s health entry — a title that ceases to exist leaves no
    residue. A missing title is a no-op (idempotent cleanup), not an error."""
    with _HEALTH_LOCK:
        _HEALTH.pop(title, None)


def retain(titles: set[str]) -> None:
    """Drop every stored title NOT in ``titles`` — the store follows the live
    manifest. Idempotent; an empty set clears all."""
    with _HEALTH_LOCK:
        for stale in _HEALTH.keys() - titles:
            del _HEALTH[stale]


def snapshot(title: str) -> dict[str, Any]:
    """The four-field health block for ``title``.

    A never-called MCP yields all four fields at their empty value
    (``consecutive_failures`` 0, the rest null); a never-failed MCP carries
    ``last_success`` with the other three at their empty value.
    """
    with _HEALTH_LOCK:
        entry = _HEALTH.get(title)
        if entry is None:
            return {"last_success": None, "last_error": None, "consecutive_failures": 0, "failing_since": None}
        return {
            "last_success": entry.last_success,
            "last_error": dict(entry.last_error) if entry.last_error is not None else None,
            "consecutive_failures": entry.consecutive_failures,
            "failing_since": entry.failing_since,
        }
