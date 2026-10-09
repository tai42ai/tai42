"""Process-wide log redaction: two registries features fill, and URL credential masking.

A feature that can leak a secret into a log line registers a redactor — a set of
substring ``markers`` and a ``redact`` function — and never installs a logging hook
of its own:

* :func:`register_record_redactor` feeds the record factory that
  :func:`install_record_redaction` chains over ``logging.getLogRecordFactory()``.
  The record is scrubbed at creation, before any handler sees it, so every sink —
  late handlers, ``propagate=False`` loggers, ``logging.lastResort`` — emits the
  redacted text.
* :func:`register_transport_redaction` feeds one filter on each of the
  :data:`TRANSPORT_LOGGERS`, which render outbound request URLs. A logger filter
  runs in ``Logger.handle`` before any handler, own or propagated.

Both apply the same rules to a record: when a redactor's marker occurs in the raw
``msg`` or ``str(args)``, the record is rendered once (``getMessage``), passed
through every matching redactor, and its ``args`` cleared so no formatter
re-interpolates the source; ``exc_text`` and ``stack_info`` are redacted when a
marker occurs in them. A marker-free record pays only the substring scan. A
redaction that raises fails the record closed: its renderable fields are replaced
by :data:`REDACTOR_FAILED`, because a hook exception would otherwise propagate into
the caller's ``log`` call and an unmasked secret must never pass.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import Final, Literal, NamedTuple

URL_REDACTION: Final = "***"
REDACTOR_FAILED: Final = "[log redactor error: record suppressed]"
TRANSPORT_LOGGERS: Final = ("httpx", "httpcore")

RecordScope = Literal["tai", "process"]

_VALID_SCOPES: Final = ("tai", "process")


class _Redactor(NamedTuple):
    markers: tuple[str, ...]
    redact: Callable[[str], str]


class _Registry:
    """Redactors by name; registering a name again replaces its entry.

    ``active`` is an immutable snapshot rebuilt on registration, so the per-record
    hooks read it without copying or locking.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, _Redactor] = {}
        self.active: tuple[_Redactor, ...] = ()

    def register(self, name: str, redactor: _Redactor) -> None:
        self._by_name[name] = redactor
        self.active = tuple(self._by_name.values())


_RECORD_REDACTORS = _Registry()
_TRANSPORT_REDACTORS = _Registry()

# Which records the installed factory scrubs. Only ever widens to "process"; the live
# wrapper reads it per record, so a widening takes effect on an installed wrapper.
_SCOPE: RecordScope = "tai"

# Marks the wrapping factory so a repeat install chains nothing.
_FACTORY_TAG: Final = "_tai42_record_redaction"

# The "tai" scope: the tai package family's module loggers (``tai42_*`` and dotted
# children), a logger named after the product (``tai`` / ``tai.*``), and the MCP
# client/server library trees the runtime drives, whose session layers log request
# content.
_TAI_SCOPE_EXACT: Final = ("tai", "mcp", "fastmcp")
_TAI_SCOPE_PREFIXES: Final = ("tai42_", "tai42.", "tai.", "mcp.", "fastmcp.")

# Renders an ``exc_info`` tuple to the text a default handler would emit.
_EXC_FORMATTER = logging.Formatter()


def _validated(kind: str, name: str, markers: tuple[str, ...], redact: Callable[[str], str]) -> _Redactor:
    if not markers:
        raise ValueError(f"{kind} redactor {name!r}: at least one marker is required")
    for marker in markers:
        if not isinstance(marker, str) or not marker:
            raise ValueError(f"{kind} redactor {name!r}: every marker must be a non-empty string, got {marker!r}")
    return _Redactor(markers=tuple(markers), redact=redact)


def _matching(redactors: Iterable[_Redactor], *texts: str) -> list[_Redactor]:
    return [r for r in redactors if any(marker in text for text in texts for marker in r.markers)]


def _apply(redactors: list[_Redactor], text: str) -> str:
    for redactor in redactors:
        text = redactor.redact(text)
    return text


def _redact_record(record: logging.LogRecord, redactors: tuple[_Redactor, ...]) -> None:
    """Scrub ``record`` in place with every redactor whose marker it carries."""
    raw_msg = record.msg if isinstance(record.msg, str) else str(record.msg)
    args_text = str(record.args) if record.args else ""
    hits = _matching(redactors, raw_msg, args_text)
    if hits:
        record.msg = _apply(hits, record.getMessage())
        record.args = None

    # ``Formatter.format`` reuses a non-empty ``exc_text`` verbatim, so the redacted
    # render replaces the raw traceback; an exception is rendered only when attached.
    if record.exc_info or record.exc_text:
        exc_text = record.exc_text or _EXC_FORMATTER.formatException(record.exc_info)  # type: ignore[arg-type]
        hits = _matching(redactors, exc_text)
        if hits:
            record.exc_text = _apply(hits, exc_text)

    if record.stack_info:
        hits = _matching(redactors, record.stack_info)
        if hits:
            record.stack_info = _apply(hits, record.stack_info)


def _fail_closed(record: logging.LogRecord) -> None:
    record.msg = REDACTOR_FAILED
    record.args = None
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None


def _redact_or_fail_closed(record: logging.LogRecord, redactors: tuple[_Redactor, ...]) -> None:
    try:
        _redact_record(record, redactors)
    except Exception:
        # Re-logging here would re-enter the hook, so the failure is carried by the
        # record itself: its text becomes REDACTOR_FAILED.
        _fail_closed(record)


def _is_tai_logger(name: str) -> bool:
    return name in _TAI_SCOPE_EXACT or name.startswith(_TAI_SCOPE_PREFIXES)


def register_record_redactor(name: str, *, markers: tuple[str, ...], redact: Callable[[str], str]) -> None:
    """Register (or replace, by ``name``) a redactor the record factory applies to in-scope records.

    Registration alone installs nothing; :func:`install_record_redaction` chains the
    factory. Raises ``ValueError`` when ``markers`` is empty or holds anything but a
    non-empty string.
    """
    _RECORD_REDACTORS.register(name, _validated("record", name, markers, redact))


def install_record_redaction(scope: RecordScope = "tai") -> None:
    """Chain one redacting wrapper over ``logging.getLogRecordFactory()``.

    ``"tai"`` scrubs the tai logger family (``tai``, ``mcp``, ``fastmcp`` and names
    starting ``tai42_``, ``tai42.``, ``tai.``, ``mcp.``, ``fastmcp.``), so an embedding
    host's own records pass untouched; ``"process"`` scrubs every record. The scope
    only widens: a later ``"tai"`` install never narrows ``"process"``. Idempotent:
    the wrapper is tagged and a repeat install chains nothing.
    """
    global _SCOPE

    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(map(repr, _VALID_SCOPES))}; got {scope!r}.")
    if scope == "process":
        _SCOPE = "process"

    previous_factory = logging.getLogRecordFactory()
    if getattr(previous_factory, _FACTORY_TAG, False):
        return

    def redacting_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)  # type: ignore[arg-type]
        redactors = _RECORD_REDACTORS.active
        if not redactors or (_SCOPE != "process" and not _is_tai_logger(record.name)):
            return record
        _redact_or_fail_closed(record, redactors)
        return record

    setattr(redacting_factory, _FACTORY_TAG, True)
    logging.setLogRecordFactory(redacting_factory)


class _TransportRedactionFilter(logging.Filter):
    """Applies every registered transport redactor; redacts, never drops."""

    def filter(self, record: logging.LogRecord) -> bool:
        redactors = _TRANSPORT_REDACTORS.active
        if redactors:
            _redact_or_fail_closed(record, redactors)
        return True


def register_transport_redaction(name: str, *, markers: tuple[str, ...], redact: Callable[[str], str]) -> None:
    """Register (or replace, by ``name``) a redactor for the :data:`TRANSPORT_LOGGERS` records.

    Installs, once, one filter on each transport logger that applies every
    registered transport redactor. Raises ``ValueError`` when ``markers`` is empty or
    holds anything but a non-empty string.
    """
    _TRANSPORT_REDACTORS.register(name, _validated("transport", name, markers, redact))
    for logger_name in TRANSPORT_LOGGERS:
        target = logging.getLogger(logger_name)
        if not any(isinstance(existing, _TransportRedactionFilter) for existing in target.filters):
            target.addFilter(_TransportRedactionFilter())


# A URL in free text. The scheme quantifier is bounded so a long run of scheme-valid
# characters not followed by ``://`` scans linearly instead of backtracking quadratically.
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]{0,62}://[^\s\"'<>]+")


def redact_url_userinfo(url: str) -> str:
    """``scheme://user:pass@host/…`` -> ``scheme://***@host/…``.

    The authority ends at the first ``/``, ``?`` or ``#``; the userinfo runs to its
    last ``@``, so an ``@`` inside a password cannot leak. A string with no ``://``
    or no ``@`` in its authority is returned unchanged.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    end = next((i for i, ch in enumerate(rest) if ch in "/?#"), len(rest))
    _userinfo, at, hostport = rest[:end].rpartition("@")
    if not at:
        return url
    return f"{scheme}://{URL_REDACTION}@{hostport}{rest[end:]}"


def _redact_url_match(match: re.Match[str]) -> str:
    url = redact_url_userinfo(match.group(0))
    base, sep, tail = url.partition("?")
    if not sep:
        return url
    query, hsep, fragment = tail.partition("#")
    pairs = []
    for pair in query.split("&"):
        key, eq, _value = pair.partition("=")
        pairs.append(f"{key}{eq}{URL_REDACTION}" if eq else pair)
    return f"{base}{sep}{'&'.join(pairs)}{hsep}{fragment}"


def redact_urls_in_text(text: str) -> str:
    """Mask every URL in free text: its userinfo and every query-string value become ``***``.

    Non-URL text is left untouched.
    """
    return _URL_RE.sub(_redact_url_match, text)


__all__ = [
    "REDACTOR_FAILED",
    "TRANSPORT_LOGGERS",
    "URL_REDACTION",
    "RecordScope",
    "install_record_redaction",
    "redact_url_userinfo",
    "redact_urls_in_text",
    "register_record_redactor",
    "register_transport_redaction",
]
