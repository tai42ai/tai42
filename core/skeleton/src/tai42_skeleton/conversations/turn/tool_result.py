"""Classify a tool run's returned envelope: a failed terminal, a suspended re-park, an
interrupt pause, or a value-free structural shape diagnostic.

Every classifier is positive discrimination on a CLOSED, exact, case-sensitive status set —
never "anything not success" — so a status from a tool's own vocabulary keeps mapping as an
ordinary payload field, and no participant content ever crosses into a log line.
"""

from __future__ import annotations

import json
from typing import Any

#: The terminal-outcome names a result envelope reports a NON-SUCCESS run with: the caller cut
#: it short, it ended early, or it failed. A result naming one of these is a failed run — its
#: ``result`` is partial (or empty) by construction, so it must never be mapped as an answer.
#:
#: EXACTLY the returned-envelope terminal vocabulary, and a CLOSED set on purpose: tools return
#: arbitrary dicts, and a ``status`` from a tool's own vocabulary (``"queued"``,
#: ``"not_found"``, ``"deleted"``) is an ordinary payload field that keeps mapping through
#: ``reply_expr``. Two near-neighbours are deliberately OUT. ``"failed"`` is a park-completion
#: FIRE status — the vocabulary a resumer names a DEFERRED terminal with (see
#: :data:`PARK_COMPLETION_FAILED`), carried beside the result rather than inside it, and read
#: on the completion-delivery path, not here. ``"errored"`` is run-STATUS REPORT vocabulary: a
#: report ABOUT a run is a successful answer to a "how did it go?" turn and must MAP — a route
#: whose whole purpose is relaying run status would otherwise fail every turn it answers
#: honestly. Matching is exact and case-sensitive for the same reason: a fuzzy or case-folded
#: match would swallow tool vocabularies that merely read like a terminal.
_FAILED_RESULT_STATUSES: frozenset[str] = frozenset({"aborted", "stopped", "error"})

#: The envelope keys naming WHY a non-success terminal failed, carried into the turn's error
#: detail in this order: the failure text and its kind, the recorded exception, the outputs the
#: run never produced, and the run handle the whole detail is traceable by. A falsy value (an
#: absent key, an empty list, a blank string) is skipped, so the detail names only what the
#: envelope actually carries. The partial ``result`` itself is deliberately excluded: the
#: detail is recorded and logged, and a partial payload belongs in neither.
_FAILED_RESULT_DETAIL_KEYS: tuple[str, ...] = ("error", "error_kind", "last_error", "missing_results", "session_id")

#: Each failure key's rendered value is capped, so an envelope carrying a large payload under
#: one of them cannot bloat the recorded detail.
_FAILED_RESULT_DETAIL_LIMIT = 200

#: Appended to a value the cap clipped, so a truncated detail never reads as a complete one.
_FAILED_RESULT_DETAIL_ELLIPSIS = "…(truncated)"


def _capped_repr(value: object) -> str:
    """``value``'s repr, clipped to :data:`_FAILED_RESULT_DETAIL_LIMIT` with an explicit
    truncation marker. Never silently: a clipped value that read as a whole one would make a
    recorded detail lie about the envelope it came from."""
    text = repr(value)
    if len(text) <= _FAILED_RESULT_DETAIL_LIMIT:
        return text
    return text[:_FAILED_RESULT_DETAIL_LIMIT] + _FAILED_RESULT_DETAIL_ELLIPSIS


def _failed_result_detail(result: object) -> str | None:
    """The internal detail for a tool result that NAMES a non-success terminal, or ``None``
    when it names none.

    A result envelope reports its own outcome: a ``status`` of :data:`_FAILED_RESULT_STATUSES`
    is a run that was aborted, stopped early, or failed. Anything else — a success, a status
    from the tool's own vocabulary, a non-string ``status``, a dict with no ``status`` at all,
    or a non-dict result — names no terminal and stays on the normal reply path.

    The detail names the terminal plus whichever of :data:`_FAILED_RESULT_DETAIL_KEYS` the
    envelope carries, each capped. It is recorded and logged, never delivered."""
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    if not isinstance(status, str) or status not in _FAILED_RESULT_STATUSES:
        return None
    reasons = [f"{key}={_capped_repr(value)}" for key in _FAILED_RESULT_DETAIL_KEYS if (value := result.get(key))]
    reason_text = f" ({'; '.join(reasons)})" if reasons else ""
    return f"tool result status {status!r}{reason_text}"


#: A run envelope can hand control back MID-RUN instead of finishing, reporting a NON-TERMINAL
#: PAUSED status rather than a success or a failure: its flagged reply surface is still
#: DOWNSTREAM of the pause (unproduced), so mapping the envelope through ``reply_expr`` would read
#: an empty/not-ready surface as though it were the produced reply — faulting an authored
#: completeness guard or delivering a blank. Two paused statuses reach here, and they take
#: DIFFERENT dispositions because they differ in one fact: whether the paused reply has a
#: DELIVERY LEG back to this turn.
#:
#: ``"suspended"`` is an async re-park whose envelope did not cross a tool-face — a consumer's own
#: resume caller keeps the run-outcome dict rather than the :class:`SuspendedInteraction` sentinel a
#: consumer's auto-pilot tool-face emits. It HAS a delivery leg: the completion continuation bound
#: around the dispatch carries this thread, so the resumed answer arrives out of band when the
#: resume drives past the pause. The turn ends SILENTLY, exactly as the :class:`SuspendedInteraction`
#: marker path does, and the recorded note reads as pending — not lost.
#:
#: ``"interrupt"`` is a tool-call pause on a run a conversation turn cannot drive, so an interrupt
#: reaching one has NO delivery leg — the reply would never arrive. That is a permanent route
#: misconfiguration (a hidden looping tool no route sensibly targets), and the standard disposition
#: for a permanent misconfiguration is LOUD: it takes the ERROR path (the same client-safe error
#: reply a failed run gets), so the failure is noticed rather than converted into silent data loss.
#:
#: POSITIVE discrimination on CLOSED, exact, case-sensitive sets — never "anything not success" —
#: for the same reason :data:`_FAILED_RESULT_STATUSES` is closed: a ``status`` from a tool's OWN
#: vocabulary is an ordinary payload field that must keep mapping. ``missing_results`` is
#: deliberately NOT the discriminator — it rides EVERY non-error envelope the engine returns, a
#: SUCCESS terminal included (as an empty list), so its presence names no pause; the status does.
#: It is read only to ENRICH the recorded detail for producers that ride ``missing_results`` on
#: paused envelopes, and is simply absent for older producers that do not.
_SUSPENDED_RESULT_STATUSES: frozenset[str] = frozenset({"suspended"})
_INTERRUPT_RESULT_STATUSES: frozenset[str] = frozenset({"interrupt"})


def _suspended_result_note(result: object) -> str | None:
    """The internal note for a tool result that NAMES a non-terminal SUSPENDED re-park
    (:data:`_SUSPENDED_RESULT_STATUSES`), or ``None`` when it names none.

    A suspended envelope is the run handing control back mid-run on an engine-caller async park,
    with its flagged reply surface still downstream of the pause, so it must never be mapped as a
    reply. The turn ends SILENTLY and the real reply delivers out of band when the resume drives
    past the pause. The note names the paused status and, for a producer that rides it, the
    ``missing_results`` surfaces the run has not produced YET, so the record can say WHY the turn
    produced no reply; a producer that does not ride it omits that cleanly. Recorded and logged,
    never delivered — the paused-run sibling of :func:`_failed_result_detail`."""
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    if not isinstance(status, str) or status not in _SUSPENDED_RESULT_STATUSES:
        return None
    missing = result.get("missing_results")
    if isinstance(missing, list) and missing:
        return f"tool run paused (status {status!r}); reply pending, missing_results={_capped_repr(missing)}"
    return f"tool run paused (status {status!r}); reply pending"


def _interrupt_result_detail(result: object) -> str | None:
    """The internal error detail for a tool result that NAMES an INTERRUPT pause
    (:data:`_INTERRUPT_RESULT_STATUSES`), or ``None`` when it names none.

    An interrupt reaching a conversation turn is a permanent route misconfiguration: the turn
    cannot drive such a run, so the pause has NO delivery leg and the reply would never
    arrive. It is surfaced as the SAME client-safe error a failed run is, so the failure is loud
    and noticed rather than silent data loss. The detail names the real cause and, for a producer
    that rides it, the ``missing_results`` surfaces the run will never produce here. Recorded and
    logged, never delivered — the loud sibling of :func:`_suspended_result_note`."""
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    if not isinstance(status, str) or status not in _INTERRUPT_RESULT_STATUSES:
        return None
    missing = result.get("missing_results")
    surfaces = f"; missing_results={_capped_repr(missing)}" if isinstance(missing, list) and missing else ""
    return (
        f"tool run paused (status {status!r}) on a conversation turn that cannot "
        f"drive it — a route misconfiguration; the reply has no delivery leg{surfaces}"
    )


#: The failure-path structural diagnostic lists at most this many key names per level, so a
#: wide envelope cannot bloat the log line.
_RESULT_SHAPE_KEY_CAP = 40


def _shape_key_names(mapping: dict[Any, Any]) -> list[str]:
    """The mapping's string keys, sorted and capped — NAMES only. An envelope key, a
    ``result`` key, or a ``return_result`` surface id is a protocol/authoring identifier, never
    participant content, so its NAME is client-safe to log; its VALUE is not and never reaches here."""
    return sorted(key for key in mapping if isinstance(key, str))[:_RESULT_SHAPE_KEY_CAP]


def _shape_surface_sizes(outputs: dict[Any, Any]) -> dict[str, int]:
    """Each ``result.outputs`` surface's NAME mapped to the approximate serialized SIZE of its
    value — never the value itself. The size is the one datum that separates an ABSENT surface
    (not here at all) from a PRESENT-BUT-EMPTY one (a size of ~2, an empty list/object), decided
    without a participant byte reaching the log. An unserializable value records ``-1`` rather than
    rendering it."""
    sizes: dict[str, int] = {}
    for name in _shape_key_names(outputs):
        try:
            sizes[name] = len(json.dumps(outputs[name], default=str))
        except Exception:
            sizes[name] = -1
    return sizes


def _result_shape(result: object) -> str:
    """A VALUE-FREE structural descriptor of a tool result, logged when the reply mapping faults
    so the next live failure yields the envelope's SHAPE as ground truth — which flagged surface
    is ABSENT vs PRESENT-BUT-EMPTY — instead of an inference from the guard's error text.

    Client-safe by construction: it emits only STRUCTURE — the type, the envelope's own key NAMES
    (protocol/authoring identifiers, never participant content), the ``status`` token, the
    ``return_result`` surface NAMES present under ``result.outputs`` with their approximate
    serialized SIZES, and the ``missing_results`` surface NAMES the run reported unproduced. A
    participant's message text lives in the VALUES under those surfaces, which this NEVER renders — only
    names, counts, and sizes cross into the log."""
    if not isinstance(result, dict):
        length = len(result) if isinstance(result, (str, bytes, list, tuple, dict, set)) else None
        return f"type={type(result).__name__}" + (f" len={length}" if length is not None else "")
    parts = [f"type=dict keys={_shape_key_names(result)}"]
    status = result.get("status")
    if isinstance(status, str):
        parts.append(f"status={status!r}")
    inner = result.get("result")
    if isinstance(inner, dict):
        parts.append(f"result_keys={_shape_key_names(inner)}")
        outputs = inner.get("outputs")
        if isinstance(outputs, dict):
            parts.append(f"outputs_surface_sizes={_shape_surface_sizes(outputs)}")
    missing = result.get("missing_results")
    if isinstance(missing, list):
        parts.append(
            f"missing_results={sorted(name for name in missing if isinstance(name, str))[:_RESULT_SHAPE_KEY_CAP]}"
        )
    return "; ".join(parts)
