"""The text of the child-side fork hook's report: how it words each stream it cannot rebuild and each failure.

The line this module draws: :mod:`tai42_kit.logging.fork_safety` decides what is rebuilt, rolled
back, closed and put back, and passes the facts of each outcome (the objects, the descriptor numbers,
the ``sys`` names and handlers); this module words every phrase the report shows. That covers the
headline, an exception's text, how a handler is named, each reason a stream is not rebuilt, each step
a failure was raised in, and where each binding is bound at the end.

The report is a headline, then each failure, a line or more each: an exception's text, then the step
it was raised in; the ``sys`` names and handlers whose streams are not rebuilt, and why; and, for a
descriptor whose rebuild recorded a failure, where each binding over it is bound and whether that
stream is open. Where the report is written is :mod:`tai42_kit.logging.fork_report_channel`.

Two of the hook's catches word the report: where reading a handler's name, or formatting an exception
the report records, raises, a fixed text stands in for that name or that exception, and what the
reading or formatting raised is not recorded. A handler is named by its type and its ``name``, never
by its ``repr``. An exception is recorded as ``traceback.format_exception_only`` formats it, by that
function's own rules, and that function catches some of what the exception's own code raises: for
example, it puts its own stand-in (``<exception str() failed>``, ``<note str() failed>``) for a
``__str__`` of the exception or of a note that raises, and reads a ``__notes__`` that raises
``AttributeError`` as absent. The hook's fixed text stands in only where that function itself raises
an ``Exception``, as when the exception's type's ``__module__`` or ``__qualname__`` raises.
"""

from __future__ import annotations

import io
import traceback

from tai42_kit.logging.stream_bindings import Binder, Binding, NotReadBack, SysName, read_stream

# How the report names an exception that ``traceback.format_exception_only`` raises formatting, as when
# the exception's type's ``__module__`` or ``__qualname__`` raises.
_UNREADABLE_EXCEPTION = "an exception whose text cannot be read"
# How the report names a handler whose class's name or ``name`` raises when read.
_UNNAMABLE_HANDLER = "a handler whose name cannot be read"


def _headline(hook: str) -> str:
    """The first line of the report of the fork hook in the module named ``hook``."""
    return f"{hook}: the child's fork hook failed on inherited streams:"


def report_text(hook: str, failures: list[str]) -> str:
    """The report of the fork hook in the module named ``hook``: its headline, then each of ``failures``.

    Every line of a failure is indented under the headline.
    """
    lines = "".join("\n  " + failure.replace("\n", "\n  ") for failure in failures)
    return f"{_headline(hook)}{lines}\n"


def _text_of(error: BaseException) -> str:
    """``error`` as ``traceback.format_exception_only`` formats it, by its own rules, or a fixed text when it raises."""
    try:
        return "".join(traceback.format_exception_only(error)).strip()
    except Exception:
        # Formatting raises, as when the exception's type's ``__module__`` or ``__qualname__`` raises. A
        # ``__str__`` that raises does not end here: the function puts its own stand-in for that text.
        return _UNREADABLE_EXCEPTION


def _failure(error: BaseException, *notes: str) -> str:
    """One failure of the report: ``error``'s text, then each of ``notes`` on a line of its own."""
    return "\n".join([_text_of(error), *notes])


def _named(binder: Binder) -> str:
    """``binder`` as the hook's messages name it: ``sys.<name>``, or a handler by its type and its ``name``."""
    if type(binder) is SysName:
        return f"sys.{binder.name}"
    try:
        return f"the {type(binder).__qualname__} named {binder.name!r}"
    except Exception:
        # The handler's class's own code raises reading its name: it is named by what it is.
        return _UNNAMABLE_HANDLER


def _written_by(bindings: list[Binding]) -> str:
    """Every ``sys`` name and handler in ``bindings``, as the hook's messages name them."""
    return ", ".join(_named(binder) for _, binder in bindings)


def not_rebuilt(bindings: list[Binding], reason: str) -> str:
    """The failure of the text streams in ``bindings`` that the hook does not rebuild, for ``reason``.

    ``reason`` is one of the reasons below (:func:`layers_reason` to :func:`not_read_back_reason`).
    """
    return _failure(TypeError(f"{_written_by(bindings)}: not rebuilt, {reason}"))


def layers_reason(buffer: object, raw: object) -> str:
    """Why a stream is not rebuilt: its buffered writer ``buffer`` over ``raw`` is of types it cannot build again."""
    return f"its buffered writer is a {type(buffer).__qualname__} over a {type(raw).__qualname__}"


def not_exact_text_reason(text: io.TextIOWrapper) -> str:
    """Why a stream is not rebuilt: ``text`` is not of the exact type ``io.TextIOWrapper``.

    It is of a subclass, or a proxy whose ``__class__`` says it is one.

    ``text``'s ``repr`` is code the hook did not write, and what it raises propagates.
    """
    return f"{text!r} is a {type(text).__qualname__}"


def byte_order_mark_reason(text: io.TextIOWrapper) -> str:
    """Why a stream is not rebuilt: ``text``'s encoding starts it with a byte-order mark the parent may have written.

    ``text``'s ``repr`` is code the hook did not write, and what it raises propagates.
    """
    return f"{text!r} starts with a byte-order mark the parent may already have written"


def shared_raw_reason() -> str:
    """Why a stream is not rebuilt: its raw layer is shared with a buffered writer that cannot be rebuilt."""
    return "its raw layer is shared with a buffered writer the hook cannot rebuild"


def not_read_back_reason(found: NotReadBack, declined: dict[int, AttributeError]) -> str:
    """Why a stream is not rebuilt: the handler ``found`` does not read back the stream rebuilt for it.

    ``found`` is what :func:`~tai42_kit.logging.stream_bindings.first_not_read_back` found. The
    handler is named with the stream it reads back, or with the ``AttributeError`` reading its
    ``stream`` raised; one in ``declined``, with the ``AttributeError`` it declined its assignment
    with too. The stream's ``repr`` is code the hook did not write, and what it raises propagates.
    """
    if found.raised is None:
        reason = f"{_named(found.handler)} reads back {found.stream!r} as its stream, not the stream rebuilt for it"
    else:
        reason = f"{_named(found.handler)} raised reading back its stream: {_text_of(found.raised)}"
    if (error := declined.get(id(found.handler))) is not None:
        reason += f"; it declined the assignment: {_text_of(error)}"
    return reason


def rebuild_failed(error: BaseException, bindings: list[Binding]) -> str:
    """The failure ``error`` raised rebuilding the text streams in ``bindings``."""
    return _failure(error, f"raised rebuilding the stream of {_written_by(bindings)}")


def stream_read_failed(error: BaseException, binder: Binder) -> str:
    """The failure ``error`` raised reading the stream of ``binder``, or a layer beneath it."""
    return _failure(error, f"raised reading the stream of {_named(binder)}")


def put_back_refused(error: BaseException, binder: Binder) -> str:
    """The failure ``error`` raised putting ``binder`` back on its inherited stream."""
    return _failure(error, f"raised putting {_named(binder)} back on its inherited stream")


def descriptor_put_back_failed(error: BaseException, fd: int) -> str:
    """The failure ``error`` raised putting descriptor ``fd`` back under its number."""
    return _failure(error, f"raised putting descriptor {fd} back")


def _close_failed(error: BaseException, layer: str, fd: int) -> str:
    """The failure ``error`` raised closing the raw layer ``layer`` names, over descriptor ``fd``."""
    return _failure(error, f"raised closing {layer} over descriptor {fd}")


def inherited_raw_close_failed(error: BaseException, fd: int) -> str:
    """The failure ``error`` raised closing the inherited raw layer a rebuild over descriptor ``fd`` replaced."""
    return _close_failed(error, "the inherited raw layer", fd)


def built_raw_close_failed(error: BaseException, fd: int) -> str:
    """The failure ``error`` raised closing the raw layer a rolled-back rebuild built over descriptor ``fd``."""
    return _close_failed(error, "the raw layer built", fd)


def raw_close_failed(error: BaseException, fd: int) -> str:
    """The failure ``error`` raised closing a raw layer over descriptor ``fd``, inherited or built."""
    return _close_failed(error, "a raw layer", fd)


def bound_now(
    binder: Binder,
    inherited: io.TextIOWrapper,
    raw: io.FileIO,
    built: tuple[io.TextIOWrapper, io.FileIO] | None,
) -> str:
    """Where ``binder`` is bound now: its ``inherited`` stream over ``raw``, the stream in ``built``, or another.

    ``built`` is the text stream the hook built in place of ``inherited`` and the raw layer under it,
    or ``None`` when it built none. Whether the stream is open is read from the ``io.FileIO`` beneath
    it that the hook holds, ``raw`` or the one in ``built``, whose ``closed`` is a member of the
    standard library's C layer and takes no lock. A handler's ``stream`` and another stream's
    ``repr`` are code the hook did not write: one that raises gives the line that the stream cannot
    be read.
    """
    try:
        stream = read_stream(binder)
        if stream is inherited:
            where, beneath = "its inherited stream", raw
        elif built is not None and stream is built[0]:
            where, beneath = "the stream rebuilt for it", built[1]
        else:
            return f"{_named(binder)} is bound to another stream, {stream!r}"
    except Exception as error:
        return f"the stream of {_named(binder)} cannot be read: {_text_of(error)}"
    return f"{_named(binder)} is bound to {where}, which is {'closed' if beneath.closed else 'open'}"
