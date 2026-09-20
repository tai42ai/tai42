"""Parse a failed boot's log into its failing handlers, error text, and routes.

Plus the external-service-only classification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HANDLER_RE = re.compile(r"(\w+): (?:\w+\.)*\w*(?:Error|Exception|Exit)\(")
_ROUTE_RE = re.compile(r"\b([A-Z]{3,7}) (/\S+?)(?=[\s'\"]|$)")


_CONNECTION_ERROR_MARKERS = (
    "ConnectError",
    "ConnectTimeout",
    "ConnectionError",
    "ConnectionRefusedError",
    "ReadTimeout",
    "ReadError",
    "PoolTimeout",
    "Transport error fetching",
    "Connection refused",
    "Connection reset",
    "All connection attempts failed",
    "Failed to establish a new connection",
    "Name or service not known",
    "Temporary failure in name resolution",
    "Network is unreachable",
    "No route to host",
    "[Errno 111]",
    "[Errno -2]",
    "[Errno -3]",
)


def _is_connection_error(text: str) -> bool:
    return any(marker in text for marker in _CONNECTION_ERROR_MARKERS)


@dataclass(frozen=True)
class BootFailure:
    """What a failed boot surfaced: the handlers that raised, their error text, and the routes named.

    So the report says exactly what broke and the classification can read each handler's
    failure class.
    """

    handlers: tuple[str, ...]
    routes: tuple[str, ...]
    detail: str
    handler_errors: tuple[tuple[str, str], ...] = ()

    def summary(self) -> str:
        parts = []
        if self.routes:
            parts.append("route(s): " + ", ".join(self.routes))
        if self.handlers:
            parts.append("failing handler(s): " + ", ".join(self.handlers))
        return "; ".join(parts) if parts else self.detail


def parse_boot_failure(log_text: str) -> BootFailure:
    """The lifecycle handlers, their error text, and routes named in a boot's failure log.

    The skeleton raises ``lifecycle handlers failed: <name>: <Exc>(...), ...`` when a
    startup handler raises; each handler name, the exception text that follows it (sliced
    up to the next handler), and any ``METHOD /path`` tokens are extracted. When no
    structured line is present the last error line is kept as the detail, so a
    non-lifecycle boot failure still reports.
    """
    marker = "lifecycle handlers failed:"
    handlers: list[str] = []
    routes: list[str] = []
    handler_errors: list[tuple[str, str]] = []
    detail = ""
    idx = log_text.rfind(marker)
    if idx != -1:
        tail = log_text[idx + len(marker) :].splitlines()[0]
        detail = f"{marker}{tail}".strip()
        handlers = list(dict.fromkeys(m.group(1) for m in _HANDLER_RE.finditer(tail)))
        routes = list(dict.fromkeys(f"{m.group(1)} {m.group(2)}" for m in _ROUTE_RE.finditer(tail)))
        spans = list(_HANDLER_RE.finditer(tail))
        for pos, match in enumerate(spans):
            end = spans[pos + 1].start() if pos + 1 < len(spans) else len(tail)
            handler_errors.append((match.group(1), tail[match.start() : end]))
    if not detail:
        error_lines = [line.strip() for line in log_text.splitlines() if re.search(r"Error|Exception|Traceback", line)]
        detail = error_lines[-1] if error_lines else "boot failed with no error line captured"
    return BootFailure(
        handlers=tuple(handlers), routes=tuple(routes), detail=detail, handler_errors=tuple(handler_errors)
    )


def external_service_handlers(failure: BootFailure) -> tuple[str, ...]:
    """The failing startup handlers whose error is a connection-class failure to an external endpoint.

    De-duplicated, in first-seen order.
    """
    return tuple(dict.fromkeys(name for name, text in failure.handler_errors if _is_connection_error(text)))


def is_external_service_only(failure: BootFailure) -> bool:
    """True when EVERY failed startup handler failed with a connection-class error (and at least one did).

    The boot got all the way to an external boundary and only the external round-trip,
    which the gate cannot complete, was unreachable. A single non-connection failure (a
    missing symbol, a refused route, a guard) makes this False, so a real candidate-core
    break is never reclassified.
    """
    return bool(failure.handler_errors) and all(_is_connection_error(text) for _name, text in failure.handler_errors)
