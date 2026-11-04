"""Logging's fork safety: the child rebuilds every inherited stream its handlers write to.

A handler writes to a text stream whose buffer, ``io.BufferedWriter`` or ``io.BufferedRandom``, holds
a C-level lock for the length of each ``write`` and ``flush``. CPython neither waits for that lock
before a fork nor reinitialises it in the child (its ``logging`` fork hooks reinitialise only the
handler locks and the module lock), so a child forked while another thread is inside ``write`` or
``flush`` inherits the lock held, and blocks forever at its own first write to that stream: a log
line, a ``print``, a traceback, the flush at interpreter exit.

:func:`install_logging_fork_safety` registers one ``os.register_at_fork`` hook, in the child only.
It runs after logging's own child hook has reinitialised the handler locks, and before every hook
registered later. For every live handler (logging's registry of handlers, ``logging.lastResort``
and handlers on no logger included) whose stream is a text stream over a buffered writer over a
file descriptor, the shape ``open`` and the interpreter's ``sys.stdout``/``sys.stderr`` make, the
hook rebuilds that buffered writer, the layer that holds the lock, once, together with the raw
layer beneath it and every text stream over that raw layer object, through it or another buffered
writer, that a handler or a name in ``sys`` (``stdout``, ``stderr``, ``__stdout__``, ``__stderr__``)
is bound to. The raw layers over one descriptor are rebuilt together:

* a new raw layer, a new buffered writer for each one over the inherited raw layer, and new text
  streams are built over the same descriptor, and every ``sys`` name and every handler bound to an
  inherited text stream is offered its new one: a ``sys`` name is set, and a handler is assigned its
  ``stream``, the way Python changes an attribute and the way ``logging.StreamHandler.setStream``
  changes this one. The hook predicts nothing about a handler from its type or its dictionary. The
  assignment runs the handler's own code (a setter, a ``__setattr__``); an ``AttributeError`` it
  raises is the handler declining the assignment, the attribute protocol's answer that its ``stream``
  is not set that way, as ``logging.lastResort`` answers, whose ``stream`` is a read-only property
  that reads ``sys.stderr``. Each handler is then read back, which runs its own code too (a property,
  a ``__getattr__``, a ``__getattribute__``), and the read-back decides: a handler must give the
  stream rebuilt for it, whether the assignment set it or it computes it from a ``sys`` name the hook
  rebound before. A failure while building, an assignment that raises anything but ``AttributeError``,
  or a handler that reads back anything else or raises reading it back, puts back every binding over
  that raw layer whose assignment returned and closes what was built, so the inherited streams are
  left as they were; the report names the handler that read back another stream, or what its
  read-back raised, and, when it declined the assignment, with what;
* once every raw layer over the descriptor is rebuilt or rolled back, the inherited raw layer of
  each one rebuilt is closed, which takes no buffer lock. While a raw layer that owns its
  descriptor is closed, the descriptor is held as one duplicate, put back at the end, so it keeps
  its number, its open file description and its inheritable flag. The inherited buffer's bytes, a
  write the parent was in the middle of, stay with the parent, which finishes that write itself;
  the closed wrapper's finaliser has nothing to do;
* each new text stream keeps its inherited one's encoding, errors, line buffering and
  write-through, the raw's mode and descriptor ownership, and every instance attribute of each
  layer (``mode``, ``name``); nothing else of the inherited streams is kept. An append mode is the
  one change: ``io.FileIO`` built in one seeks the descriptor to its end, which moves the offset the
  parent shares, so a raw in ``ab`` or ``ab+`` is built in ``wb`` or ``wb+`` and its ``mode`` reads
  ``wb`` or ``rb+``; its writes go where the inherited raw layer's went, as ``O_APPEND`` belongs to
  the open file description: to the end when the description carries it, as ``open(path, "a")``
  sets it, else to the offset the parent shares. A text stream shared by several handlers and names
  stays shared, text streams that shared a buffer share the new one, and buffered writers that
  shared a raw layer share the new one.

The line this module draws: it is the hook's procedure, which decides what is rebuilt, rolled back,
closed and put back, and records each outcome by its facts (the objects, the descriptor numbers, the
``sys`` names and handlers). Every phrase the report shows, each reason a stream is not rebuilt and
each step a failure was raised in included, is worded by :mod:`tai42_kit.logging.fork_report_text`.

Nothing waits for another thread, before or after the fork, and nothing is held across it: the
parent's threads keep logging while the fork happens, and the child's hook touches only its own,
single-threaded copy of the streams. The cyclic garbage collector does not run while the hook does,
so no finaliser (a ``__del__`` that logs) meets a stream mid-rebuild. A fork from inside a handler's
``emit``, from a signal handler, from several threads at once, nested in another fork's hooks, or
while ``dictConfig`` runs on another thread, is no different from any other fork: the child rebuilds
what it inherited. The hook runs for every fork that runs the interpreter's fork hooks: ``os.fork``,
``os.forkpty``, ``multiprocessing``'s fork start method and billiard's, and ``subprocess`` with a
``preexec_fn`` (without one, the child runs no Python before it executes the new program).

What holds: after the child's fork hooks have run, no stream the hook rebuilt carries a lock from the
parent, so the child's writes to them complete and never carry the bytes of a line the parent was
in the middle of; no handler and no ``sys`` name is left bound to a stream the hook closed, unless
reading a handler's ``stream``, or a layer of none of the standard library's types beneath it,
raises, putting a descriptor back fails, or a handler's own code, run when the hook sets its
``stream``, raises after it set the stream, or raises or does nothing when it is put back on its
inherited stream (below).

What does not:

* A buffered writer the hook cannot rebuild, or a text stream over it the hook cannot rebind, is
  left untouched with every text stream over it and every other buffered writer over its raw layer,
  and keeps the inherited lock, as in plain CPython:
  a buffered writer over anything but an ``io.FileIO`` (a socket's ``makefile``, a raw written in
  Python); a subclass of ``io.TextIOWrapper``, ``io.BufferedWriter``, ``io.BufferedRandom`` or
  ``io.FileIO``, or a proxy whose ``__class__`` says it is one of them, whose own construction the
  hook cannot repeat; an encoding that starts a stream with
  a byte-order mark (``utf-8-sig``, ``utf-16``, ``utf-32``) over a descriptor that cannot seek or sits
  at its start, where whether the parent already wrote the mark cannot be read; a handler that does
  not read back the stream rebuilt for it: one that reads back another object, or raises
  ``AttributeError`` reading it back.
* A text stream or a buffered writer of the standard library's types, of a subclass too, is told apart
  by its type, past any ``__class__`` of its own, and the layer beneath it read past any ``buffer`` or
  ``raw`` of its own (:mod:`tai42_kit.logging.stream_layers`), so those reads run none of its code and
  raise nothing. A stream or a buffered writer of none of these types is told apart by its own
  ``__class__``, its own code, which may raise; one that says by it that it is an ``io.TextIOWrapper``,
  ``io.BufferedWriter`` or ``io.BufferedRandom`` (a proxy) has the layer beneath it read by its own
  ``buffer`` or ``raw``, its own code too. Python's own rule reads an ``AttributeError`` from two of
  these reads as an attribute that is absent: a handler whose ``stream`` raises it has no ``stream``,
  by ``getattr``'s rule, as a ``logging.NullHandler`` and a bare ``logging.Handler`` have none, and an
  object whose own ``__class__`` raises it does not stand for a stream, by ``isinstance``'s rule;
  either is passed over unreported. Any other ``Exception`` reading a handler's ``stream`` or an
  object's own ``__class__``, and any ``Exception`` reading a proxy's ``buffer`` or ``raw``, is named
  in the report with the ``sys`` name or handler it was read for, which is left bound as it is.
  Beneath what is passed over, or whose read raised, the hook reads nothing: a raw layer it rebuilds
  for another handler is closed beneath it, as beneath a reference held elsewhere (below). A handler
  whose read-back raises ``AttributeError`` does not read back the stream rebuilt for it (above), and
  the report names what it raised.
* A step that fails (a stream whose ``repr`` raises while it is told apart, ``os.dup`` at the
  descriptor limit) leaves its stream as it was, and the hook goes on with the rest; a failure putting
  its descriptor back, and a handler whose own code raises after it set its ``stream`` or sets nothing
  when it is put back, are the exceptions (below). After every other stream is rebuilt, the hook
  writes one report naming each stream it cannot rebuild or rebind and each failure, with the step it
  failed in, and, for each descriptor whose rebuild recorded a failure, where every ``sys`` name and
  handler over it is bound and whether that stream is open or closed, each read once the work on that
  descriptor is done. The hook raises no ``Exception``: the interpreter writes a fork hook's exception
  through ``sys.stderr``'s own code, which may wait on a lock a thread of the parent held at the fork,
  the wait the hook exists to end. The hook runs code of objects it does not own only through each
  object's public interface, and never predicts from an object's type or dictionary what its code will
  do. A layer of the standard library's own stream types, a subclass's included, is told apart by its
  type and read through that type's C members (:mod:`tai42_kit.logging.stream_layers`): those reads
  run none of the stream's code and take no lock. An object of none of those types is asked by its own
  ``__class__`` whether it stands for one, and one that says so (a proxy) is asked by its own
  ``buffer`` or ``raw`` what is beneath it. A handler is spoken to as ``logging.StreamHandler``
  defines its stream: its ``stream`` is read, assigned and read back
  (:mod:`tai42_kit.logging.stream_bindings`), its class is named by its ``__qualname__`` and the
  handler by its ``name``. Every such call, and every other call the hook makes into code it did not
  write (a stream's ``repr``, a raw layer's ``flush`` its close runs, a codec, the formatting of an
  exception), runs where an ``Exception`` it raises is caught. Of the hook's own catches outside its
  report's channel (below), all but two record what they catch in the report as text, unless one of
  three rules makes the answer not a failure: an ``AttributeError`` that Python's own rule reads as an
  attribute that is absent, from a handler's ``stream`` or an object's own ``__class__`` read before
  any assignment, passes the binding over (above); an ``AttributeError`` from the assignment of a
  handler's ``stream`` is the handler declining it, after which the read-back decides; and the
  ``EBADF`` closing a descriptor's number the hook has already freed, which a raw layer that owns the
  descriptor raises once another one closed the number, ends with nothing open over the number, the
  end the close is for. The other two catches, where reading a handler's name or formatting an
  exception raises, word the report: they belong to the report's text,
  :mod:`tai42_kit.logging.fork_report_text`, which describes them and how the report names a handler
  and records an exception. The report's channel is
  :mod:`tai42_kit.logging.fork_report_channel`, where what writing the report raises leaves it
  written in part or not at all, unrecorded, as that module describes.
* Putting a descriptor back (``os.dup2``) can fail: on Linux with ``EBUSY``, when another thread of the
  child is opening a file under that number at that moment (see the thread started by an earlier hook,
  below). The number is then free, so that nothing writes into whatever file next takes it, every raw
  layer over it under a buffered writer bound to a handler or a ``sys`` name, an ``io.FileIO`` of that
  exact type, inherited or built, is closed, which closes whatever the number names at that moment. That
  takes in the buffered writers rolled back, those only ``sys`` names write to and those of a subclass,
  not only the ones rebuilt. Every handler and ``sys`` name over the descriptor is bound to its
  inherited stream, now closed (a write raises ``ValueError``), a handler whose own code raises when its
  ``stream`` is set or sets nothing when it is put back excepted (below), and the report names the
  failure, then where each binding over the descriptor is bound and whether that stream is open, each
  read once the work on the descriptor is done. A ``sys.stderr`` over that descriptor is closed with the
  rest, and takes no report (:mod:`tai42_kit.logging.fork_report_channel`). A stream over the number the
  hook does not reach that way (a text stream with no buffered writer, a raw layer of a subclass of
  ``io.FileIO``, a reference held elsewhere) stays open over it. From the first close of a raw layer
  that owns a descriptor until the descriptor is put back, its number is free: a later close of another
  raw layer that owns it closes whatever the number names then, and a file another thread of the child
  opens under the number in that time, its open complete, is closed by the put-back, which reports
  nothing: ``os.dup2`` closes what the number names and puts the descriptor back under it, so that
  thread's writes through the number go to the descriptor.
* Setting a handler's ``stream``, to the stream rebuilt for it or, on a rollback or after a failed
  put-back, back to its inherited one, runs the handler's own code (a ``stream`` setter, a
  ``__setattr__``), which can raise, or set nothing. On the way to the rebuilt stream an
  ``AttributeError`` is the handler declining, and it is left as the raise leaves it; a handler whose
  code sets ``stream`` and then raises ``AttributeError`` has declined too, is not put back, and stays
  on the stream rebuilt for it, closed when that raw layer's rebuild is rolled back. Only a binding
  whose set to a rebuilt stream returned is put back, and once. On the way back every raise, an
  ``AttributeError`` included, is a failure: the handler accepted the stream rebuilt for it and now
  refuses its own, and is left as the raise leaves it, on a stream the hook closes; one whose code
  does not set it stays on the rebuilt stream, closed too. The report names each raise, then reads
  where each binding is bound and whether that stream is open. Every other binding is still put back
  and every other raw layer over the descriptor still rebuilt.
* A rebuilt text stream has the default ``newline`` and its buffered writer the default buffer size:
  no interface reads either from the inherited stream.
* A stream of any other shape (a ``StringIO``, an unbuffered text stream, a binary stream) has no such
  lock and is left as it is, unreported unless reading it raises an ``Exception`` that is a failure,
  which the report names (above). So is a closed or detached stream: a write through its text layer
  raises ``ValueError`` before it reaches the lock.
* A stream over a raw layer no handler writes through (``sys.stdout`` when no handler uses it, a file
  a thread writes with ``print``) is not rebuilt; a thread inside its ``write`` at the fork leaves its
  lock held in the child, as in plain CPython.
* A reference to an inherited text stream held elsewhere (a variable, another object's attribute), and a
  text stream over a rebuilt buffer, or over another buffer over a rebuilt raw layer, that no handler
  and no ``sys`` name is bound to, now points at a closed stream: a ``write`` or a ``flush`` through it
  raises ``ValueError``, not a hang, and a ``close`` returns at once. A ``close`` on its buffer object
  blocks on the inherited lock.
* A thread started in the child by a fork hook registered before this one runs alongside the hook.
  Logging before the rebuild, it can block on the inherited lock, holding its handler's lock; the
  child is then stuck as plain CPython would be. Logging while the hook runs, it can meet a stream
  between its close and its rebind: the write raises ``ValueError``, which logging reports as a
  logging error. Opening a file while the hook runs, it can be given the number of a descriptor the
  hook has freed until its put-back: an open still in progress at the put-back makes it fail with
  ``EBUSY``, and an open complete by then has its file closed by the put-back without a report, the
  thread's writes through the number going to the descriptor put back (above). Hooks registered
  after this one run after the rebuild.
* A Python signal handler that runs in the child while the hook runs sees the streams mid-rebuild.
* Forks made outside the interpreter's fork hooks (a C library calling ``fork``) run no hook.
* The hook rests on one private name of the standard library, ``logging._handlerList``, the
  registry of live handlers that ``logging.shutdown`` itself walks; it is read at import, so a
  rename fails at process start.
"""

from __future__ import annotations

import codecs
import dataclasses
import errno
import gc
import io
import logging
import os
import sys
import threading
import weakref
from collections.abc import Callable

from tai42_kit.logging.fork_report_channel import write_report
from tai42_kit.logging.fork_report_text import (
    bound_now,
    built_raw_close_failed,
    byte_order_mark_reason,
    descriptor_put_back_failed,
    inherited_raw_close_failed,
    layers_reason,
    not_exact_text_reason,
    not_read_back_reason,
    not_rebuilt,
    put_back_refused,
    raw_close_failed,
    rebuild_failed,
    report_text,
    shared_raw_reason,
    stream_read_failed,
)
from tai42_kit.logging.stream_bindings import (
    SYS_NAMES,
    Binder,
    Binding,
    SysName,
    bind,
    first_not_read_back,
    put_back,
    stream_of,
)
from tai42_kit.logging.stream_layers import (
    Buffer,
    RawLayer,
    buffer_beneath,
    exact_buffer,
    is_buffer,
    is_text,
    open_file_io,
    raw_beneath,
)

_handler_refs: list[weakref.ReferenceType[logging.Handler]] = logging._handlerList  # pyright: ignore[reportAttributeAccessIssue]

_installed = False
_install_lock = threading.Lock()


def install_logging_fork_safety() -> None:
    """Register the child-side fork hook that rebuilds the handlers' inherited streams.

    ``os.register_at_fork`` cannot be undone, so the hook is registered once per process; a repeat
    call changes nothing.
    """
    global _installed
    with _install_lock:
        if _installed:
            return
        os.register_at_fork(after_in_child=_after_in_child)
        _installed = True


def _after_in_child() -> None:
    """Rebuild every buffered writer a live handler writes to, with its raw layer, and rebind its bindings.

    One raw layer's failure leaves its streams as they were and stops nothing else; after every other
    one is rebuilt, one report names each failure and each stream that cannot be rebuilt or
    rebound (:func:`~tai42_kit.logging.fork_report_text.report_text`). The hook raises no
    ``Exception``: each call it makes into code it did not write runs where what it raises is
    caught, and recorded in ``failures`` as text unless the module's three rules make the answer not
    a failure: an ``AttributeError`` that Python's own rule reads as an attribute that is absent (a
    handler's ``stream`` by ``getattr``'s, an object's own ``__class__`` by ``isinstance``'s), an
    ``AttributeError`` from the assignment of a handler's ``stream``, the handler declining it, and
    the ``EBADF`` closing a descriptor's number the hook has already freed. Where reading a handler's
    name or formatting an exception raises, a fixed text stands in instead
    (:mod:`tai42_kit.logging.fork_report_text`). What writing the report raises is its channel's
    (:func:`write_report`).
    """
    failures: list[str] = []
    collecting = gc.isenabled()
    gc.disable()
    try:
        for group in _grouped_by_descriptor(_bound_buffers(failures)):
            if isinstance(group, _OverDescriptor):
                _rebuild_descriptor(group, failures)
            else:
                _record_unrebuildable(group, failures)
        if failures:
            write_report(report_text(__name__, failures))
    finally:
        if collecting:
            gc.enable()


def _a_handler_writes(bindings: list[Binding]) -> bool:
    """Whether a handler, not only ``sys`` names, is bound in ``bindings``."""
    return any(type(binder) is not SysName for _, binder in bindings)


@dataclasses.dataclass(slots=True)
class _Bound:
    """A buffered writer the hook reached, its raw layer, and every ``sys`` name and handler bound over it."""

    buffer: Buffer
    raw: RawLayer | None  # ``None`` once the buffer is detached
    bindings: list[Binding]


@dataclasses.dataclass(slots=True)
class _OverRaw:
    """Every buffered writer the hook reached over one open ``io.FileIO`` raw layer of that exact type."""

    raw: io.FileIO
    buffers: list[_Bound]

    @property
    def bindings(self) -> list[Binding]:
        """Every ``sys`` name and handler bound over the raw layer, buffered writer by buffered writer."""
        return [binding for bound in self.buffers for binding in bound.bindings]


@dataclasses.dataclass(slots=True)
class _OverDescriptor:
    """Every buffered writer the hook reached over one open descriptor, gathered by its ``io.FileIO`` raw layer."""

    fd: int
    raws: list[_OverRaw]


def _bound_buffers(failures: list[str]) -> list[_Bound]:
    """Each buffered writer with every ``sys`` name, then every live handler, bound to a text stream over it.

    Each ``sys`` name and each handler's ``stream`` is read once, and the layers beneath it through
    :mod:`tai42_kit.logging.stream_layers`. An ``AttributeError`` that Python's own rule reads as an
    attribute that is absent leaves a binding out, not recorded: a handler whose ``stream`` raises it
    has no ``stream`` (:func:`stream_of`), by ``getattr``'s rule, and an object whose own ``__class__``
    raises it is, by ``isinstance``'s rule, of none of the standard library's stream types. Any other
    ``Exception`` reading a handler's ``stream`` or an object's own ``__class__``, and any
    ``Exception`` reading a proxy's ``buffer`` or ``raw``, is recorded in ``failures`` and the binding
    left out; the others are read on.
    """
    binders: list[Binder] = [*SYS_NAMES]
    binders += [handler for ref in list(_handler_refs) if (handler := ref()) is not None]
    by_buffer: dict[int, _Bound] = {}
    for binder in binders:
        try:
            layered = _text_over_buffer(stream_of(binder))
            raw = None if layered is None else raw_beneath(layered[1])
        except Exception as error:
            failures.append(stream_read_failed(error, binder))
            continue
        if layered is not None:
            text, buffer = layered
            if id(buffer) not in by_buffer:
                by_buffer[id(buffer)] = _Bound(buffer, raw, [])
            by_buffer[id(buffer)].bindings.append((text, binder))
    return list(by_buffer.values())


def _grouped_by_descriptor(bound: list[_Bound]) -> list[_Bound | _OverDescriptor]:
    """``bound`` in the order read, the buffered writers over one open ``io.FileIO`` descriptor gathered into one group.

    A buffered writer whose raw layer is an open ``io.FileIO`` of that exact type joins the group of
    its descriptor (:func:`~tai42_kit.logging.stream_layers.open_file_io`), with the others over that
    raw layer object; any other stands alone.
    """
    groups: list[_Bound | _OverDescriptor] = []
    by_fd: dict[int, _OverDescriptor] = {}
    by_raw: dict[int, _OverRaw] = {}
    for each in bound:
        opened = open_file_io(each.raw)
        if opened is None:
            groups.append(each)
            continue
        raw, fd = opened
        if fd not in by_fd:
            by_fd[fd] = _OverDescriptor(fd, [])
            groups.append(by_fd[fd])
        if id(raw) not in by_raw:
            by_raw[id(raw)] = _OverRaw(raw, [])
            by_fd[fd].raws.append(by_raw[id(raw)])
        by_raw[id(raw)].buffers.append(each)
    return groups


def _text_over_buffer(stream: object) -> tuple[io.TextIOWrapper, Buffer] | None:
    """``stream`` and the buffered writer under it, the layer that holds the lock; ``None`` for any other stream.

    Each layer is told apart, and the buffered writer read, through the standard library's own
    layers (:mod:`tai42_kit.logging.stream_layers`).
    """
    if is_text(stream):
        buffer = buffer_beneath(stream)  # ``None`` once the text stream is detached
        if is_buffer(buffer):
            return stream, buffer
    return None


def _record_unrebuildable(bound: _Bound, failures: list[str]) -> None:
    """Record in ``failures`` that a buffered writer a handler writes to, over no open ``io.FileIO``, is not rebuilt.

    A closed or detached buffer is left as it is, unrecorded: a write through its text layer raises
    ``ValueError`` before it reaches the lock.
    """
    if not _a_handler_writes(bound.bindings):
        return
    try:
        if bound.raw is None or bound.raw.closed:
            return
        failures.append(not_rebuilt(bound.bindings, layers_reason(bound.buffer, bound.raw)))
    except Exception as error:
        failures.append(rebuild_failed(error, bound.bindings))


def _rebuild_descriptor(over: _OverDescriptor, failures: list[str]) -> None:
    """Rebuild every raw layer over one descriptor that a handler writes through; record why not when one cannot be.

    A raw layer is rebuilt with every buffered writer over it (:class:`_RawRebuild`): one only ``sys``
    names write through is left as it is, one with a buffered writer that cannot be rebuilt or
    rebound is left as it is and recorded (:func:`_not_rebuildable`), and the rest are rebuilt
    together (:class:`_DescriptorRebuild`). When they are and the descriptor has recorded a failure,
    its failures are followed in ``failures`` by its end state, read once the work on it is done
    (:func:`_end_state`).
    """
    group: list[str] = []
    rebuilds: list[_RawRebuild] = []
    for over_raw in over.raws:
        if not _a_handler_writes(over_raw.bindings):
            continue
        if reasons := _not_rebuildable(over_raw):
            group += reasons
        else:
            rebuilds.append(_RawRebuild(over_raw))
    if rebuilds:
        try:
            _DescriptorRebuild(over, rebuilds, group).run()
        except Exception as error:
            group.append(rebuild_failed(error, [binding for rebuild in rebuilds for binding in rebuild.bindings]))
        if group:
            group += _end_state(over, rebuilds)
    failures += group


def _end_state(over: _OverDescriptor, rebuilds: list[_RawRebuild]) -> list[str]:
    """Where every ``sys`` name and handler over the descriptor is bound now, and whether that stream is open.

    One line per binding, in the order the bindings were read, given its inherited stream, the raw
    layer under it, and the text stream and raw layer built in their place, if any
    (:func:`~tai42_kit.logging.fork_report_text.bound_now`).
    """
    by_raw = {id(rebuild.raw): rebuild for rebuild in rebuilds}
    lines: list[str] = []
    for over_raw in over.raws:
        rebuild = by_raw.get(id(over_raw.raw))
        for text, binder in over_raw.bindings:
            built = None if rebuild is None else rebuild.built_over(text)
            lines.append(bound_now(binder, text, over_raw.raw, built))
    return lines


def _not_rebuildable(over: _OverRaw) -> list[str]:
    """Why the buffered writers over ``over``'s raw layer cannot be built again, one failure each; empty when all can.

    One that cannot (:func:`_cannot_rebuild`), or raises while it is told apart, keeps the raw layer
    open beneath it, so every other one over the raw layer is not rebuilt either.
    """
    failures: list[str | None] = []
    for bound in over.buffers:
        try:
            reason = _cannot_rebuild(bound.buffer, over.raw, bound.bindings)
        except Exception as error:
            failures.append(rebuild_failed(error, bound.bindings))
        else:
            failures.append(None if reason is None else not_rebuilt(bound.bindings, reason))
    if not any(failures):
        return []
    shared = shared_raw_reason()
    pairs = zip(failures, over.buffers, strict=True)
    return [recorded or not_rebuilt(bound.bindings, shared) for recorded, bound in pairs]


def _cannot_rebuild(buffer: Buffer, raw: io.FileIO, bindings: list[Binding]) -> str | None:
    """Why ``buffer`` over ``raw``, with the text streams in ``bindings``, cannot be built again, or ``None``."""
    if not exact_buffer(buffer):
        return layers_reason(buffer, raw)
    for text, _ in bindings:
        if type(text) is not io.TextIOWrapper:
            return not_exact_text_reason(text)
        if _writes_a_byte_order_mark(text) and not (raw.seekable() and raw.tell() != 0):
            return byte_order_mark_reason(text)
    return None


def _writes_a_byte_order_mark(text: io.TextIOWrapper) -> bool:
    """Whether ``text``'s encoding writes a byte-order mark at the start of a stream."""
    return codecs.getincrementalencoder(text.encoding)().encode("") != b""


class _RawRebuild:
    """One raw layer's new layers over its descriptor, every buffered writer over it included, bound or rolled back.

    The buffered writers that shared the inherited raw layer share the one built, and the text
    streams that shared a buffered writer share the one built for it. The layers it builds are held
    here, so the finaliser of a rolled-back layer cannot close the descriptor while the other raw
    layers over it are still being built; the descriptor's rebuild closes its raw layer. ``moved``
    is the undo log: each binding whose set to its rebuilt stream returned and that is not yet put
    back.
    """

    def __init__(self, over: _OverRaw) -> None:
        self.raw = over.raw
        self.buffers = over.buffers
        self.bindings = over.bindings
        self.built_raw: io.FileIO | None = None
        self.rebuilt = False
        self.moved: list[Binding] = []
        self._built_buffers: list[Buffer] = []
        self._built: dict[int, io.TextIOWrapper] = {}

    def built_over(self, inherited: io.TextIOWrapper) -> tuple[io.TextIOWrapper, io.FileIO] | None:
        """The text stream built in place of ``inherited`` and the raw layer built under it; ``None`` if none was."""
        built = self._built.get(id(inherited))
        if built is None or self.built_raw is None:
            return None
        return built, self.built_raw

    def make(self, fd: int, failures: list[str]) -> None:
        """Build the new layers over ``fd``, bind them and read every handler back; unless all hold, roll back.

        Binding stops at the first ``sys`` name or handler whose set raises, a handler's
        ``AttributeError``, its decline, excepted (:func:`~tai42_kit.logging.stream_bindings.bind`),
        and the rollback follows. A rollback puts back on its inherited stream each binding in
        ``moved``, the ones whose set returned, and only those (:func:`_bind_back`); the raw layer
        built is left for the descriptor's rebuild to close. Each failure is recorded in ``failures``,
        the one that rolls the rebuild back first.
        """
        try:
            self.built_raw = io.FileIO(fd, _without_append(self.raw.mode), closefd=self.raw.closefd)
            vars(self.built_raw).update(vars(self.raw))
            for bound in self.buffers:
                buffer = type(bound.buffer)(self.built_raw)
                self._built_buffers.append(buffer)
                vars(buffer).update(vars(bound.buffer))
                for key, text in {id(text): text for text, _ in bound.bindings}.items():
                    self._built[key] = _rewrapped(text, buffer)
            declined = bind(self.bindings, self._built, self.moved)
            found = first_not_read_back(self.bindings, self._built)
            if found is not None:
                failures.append(not_rebuilt(self.bindings, not_read_back_reason(found, declined)))
            self.rebuilt = found is None
        except Exception as error:
            failures.append(rebuild_failed(error, self.bindings))
        finally:
            if not self.rebuilt:
                _bind_back(self.moved, failures)


class _DescriptorRebuild:
    """The rebuild of the raw layers over one descriptor: each made whole, or rolled back whole.

    Each raw layer's rebuild in ``rebuilds`` is made (:meth:`_RawRebuild.make`). Then the raw layers
    they replace are closed: the inherited one of each rebuilt, the built one of each rolled back. A
    raw layer that owns its descriptor closes it, so when one in ``rebuilds`` owns it the descriptor
    is held as one duplicate meanwhile and put back under its number, with its inheritable flag, at
    the end. When that fails the number is free and may name another file at any moment, so every
    raw layer in ``over``, inherited or built, the ones only ``sys`` names write through included, is
    closed, and every binding still in an undo log (``moved``) is put back on its inherited stream,
    each one that raises recorded and left as the raise leaves it. Each failure is recorded in
    ``failures`` by the step it is raised in.
    """

    def __init__(self, over: _OverDescriptor, rebuilds: list[_RawRebuild], failures: list[str]) -> None:
        self._over = over
        self._rebuilds = rebuilds
        self._failures = failures
        self._fd = over.fd
        self._inheritable = os.get_inheritable(self._fd)
        # Whether a raw layer that owns the descriptor has been closed, which frees its number.
        self._freed = False

    def run(self) -> None:
        """Make every raw layer's rebuild, close the raw layers they replace, and put the descriptor back."""
        keep = os.dup(self._fd) if any(rebuild.raw.closefd for rebuild in self._rebuilds) else None
        try:
            for rebuild in self._rebuilds:
                rebuild.make(self._fd, self._failures)
        finally:
            try:
                self._close_replaced()
            finally:
                if keep is not None:
                    self._put_back(keep)

    def _close_replaced(self) -> None:
        """Close the raw layer each rebuild replaces: the inherited one if rebuilt, else the one built."""
        for rebuild in self._rebuilds:
            if rebuild.rebuilt:
                self._close_recorded(rebuild.raw, inherited_raw_close_failed)
            elif rebuild.built_raw is not None:
                self._close_recorded(rebuild.built_raw, built_raw_close_failed)

    def _put_back(self, keep: int) -> None:
        """Put the duplicate ``keep`` back under the descriptor's number, and close it.

        If that fails, the failure is recorded, every raw layer over the number is closed
        (:meth:`_close_every_raw`) and every binding still in an undo log (``moved``) is put back on
        its inherited stream (:meth:`_bind_inherited`).
        """
        try:
            os.dup2(keep, self._fd, inheritable=self._inheritable)
        except OSError as error:
            self._failures.append(descriptor_put_back_failed(error, self._fd))
            self._close_every_raw()
            self._bind_inherited()
        finally:
            os.close(keep)

    def _close_every_raw(self) -> None:
        """Close every raw layer over the descriptor, built and inherited."""
        raws = [rebuild.built_raw for rebuild in self._rebuilds if rebuild.built_raw is not None]
        raws += [over_raw.raw for over_raw in self._over.raws]
        for raw in raws:
            self._close_recorded(raw, raw_close_failed)

    def _bind_inherited(self) -> None:
        """Put each binding still in an undo log (``moved``) back on its inherited stream (:func:`_bind_back`)."""
        for rebuild in self._rebuilds:
            _bind_back(rebuild.moved, self._failures)

    def _close_recorded(self, raw: io.FileIO, close_failed: Callable[[BaseException, int], str]) -> None:
        """Close ``raw`` (:meth:`_close_over_number`); what it raises is recorded as ``close_failed`` words it."""
        if (error := self._close_over_number(raw)) is not None:
            self._failures.append(close_failed(error, self._fd))

    def _close_over_number(self, raw: io.FileIO) -> BaseException | None:
        """Close ``raw`` (:func:`_close`), and return what the close raised, or ``None``.

        A raw layer that owns the descriptor, closed after another one has freed its number, raises
        ``EBADF`` closing the number, and that is not returned: the raw is closed afterwards, and
        nothing is open over the number, which is the end the close is for. What its ``flush`` raised
        before, the ``EBADF``'s context, is returned in its place. A raw layer already closed closes
        again at once, raising nothing.
        """
        owns = raw.closefd
        # The exception being handled while the close runs, which an ``EBADF`` with nothing raised
        # before it in the close carries as its context.
        handling = sys.exception()
        error: BaseException | None = _close(raw)
        if owns:
            if self._freed and type(error) is OSError and error.errno == errno.EBADF:
                error = None if error.__context__ is handling else error.__context__
            self._freed = True
        return error


def _close(raw: io.FileIO) -> Exception | None:
    """Close ``raw`` by ``io.FileIO``'s own ``close``, past any instance attribute of that name; return what it raised.

    ``raw`` is closed afterwards whatever the close raises, a ``flush`` it runs included.
    """
    try:
        io.FileIO.close(raw)
    except Exception as error:
        return error
    return None


def _bind_back(moved: list[Binding], failures: list[str]) -> None:
    """Put each ``sys`` name and handler in ``moved`` back on its inherited stream, recording each refusal.

    The put-back is :func:`~tai42_kit.logging.stream_bindings.put_back`; each that refuses, a
    handler's own code raising, is recorded in ``failures`` under its own name.
    """
    for binder, error in put_back(moved):
        failures.append(put_back_refused(error, binder))


def _without_append(mode: str) -> str:
    """``mode`` with append (``a``) made write (``w``): the mode a raw layer is built in over an inherited descriptor.

    ``io.FileIO``'s constructor in an append mode seeks its descriptor to the end, which moves the
    offset of the open file description the parent shares. Over a descriptor, the mode sets only
    which of read and write the raw layer allows (``w`` truncates only a file the constructor opens
    by name), and a write goes where the inherited raw layer's went, as ``O_APPEND`` is a flag of the
    open file description: to the end when the description carries it.
    """
    return mode.replace("a", "w")


def _rewrapped(text: io.TextIOWrapper, buffer: Buffer) -> io.TextIOWrapper:
    """A text stream over ``buffer`` with ``text``'s encoding, errors, buffering and instance attributes."""
    new = io.TextIOWrapper(
        buffer,
        encoding=text.encoding,
        errors=text.errors,
        line_buffering=text.line_buffering,
        write_through=text.write_through,
    )
    vars(new).update(vars(text))
    return new
