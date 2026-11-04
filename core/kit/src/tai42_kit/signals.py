"""The process-wide composed asyncio signal chain.

``loop.add_signal_handler`` is LAST-WRITER-WINS and exposes no read-back of the
callback it replaces, so two components that each install one for the same
signal silently destroy each other: whichever ran second owns the signal, and
the first component's handler is gone with nothing reporting it. The matching
``loop.remove_signal_handler`` is just as blunt — it restores SIGTERM to
``SIG_DFL`` (asyncio restores ``default_int_handler`` only for SIGINT), so the
component that leaves first strips the signal from every component that stayed.

Composition is only possible if BOTH sides register through one owner. This
module is that owner: ONE asyncio handler per signal, many independent
subscribers, LIFO delivery, and the handler uninstalled only when its LAST
subscriber leaves.

A body that blocks the loop thread can run no subscriber until it returns, so a
signal the chain owns would wait the body out. For such a span the chain
releases its signals (:meth:`SignalChain.released`): the process's default
dispositions are in force, and the chain takes the signals back when the span
ends. It also names the pair a process is stopped with, :data:`STOP_SIGNALS`.

Pure stdlib, so a process that never subscribes pays one unused module.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Final

logger = logging.getLogger(__name__)

STOP_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT)
"""The two signals a process is stopped with: the orchestrator's ``SIGTERM`` and the terminal's ``SIGINT``."""


class SignalSubscription:
    """One component's place in the chain for one signal.

    Held by the subscriber and cancelled on its way out; cancelling is the only
    thing that can shrink the chain, so a subscriber that never cancels keeps
    the signal wired for the process lifetime (which is what a launcher wants).
    """

    __slots__ = ("_cancelled", "_chain", "callback", "name", "sig")

    def __init__(self, chain: SignalChain, sig: signal.Signals, callback: Callable[[], object], name: str) -> None:
        """Record the owning ``chain``, the ``sig`` subscribed to, the ``callback``, and its ``name``."""
        self._chain = chain
        self.sig = sig
        self.callback = callback
        self.name = name
        self._cancelled = False

    def cancel(self) -> None:
        """Leave the chain.

        Idempotent: a subscriber unwinding through several ``finally`` blocks must
        not have to track whether it already left.
        """
        if self._cancelled:
            return
        self._cancelled = True
        self._chain._remove(self)

    def __repr__(self) -> str:
        """Name and signal of this subscription, for logs and diagnostics."""
        return f"<SignalSubscription {self.name!r} on {self.sig.name}>"


class SignalChain:
    """Composed asyncio signal handling for one process.

    Loop-thread-affine by construction — ``asyncio`` accepts signal-handler
    registration only from the loop's own (main) thread — so the subscriber
    tables need no lock: every mutation and every dispatch runs on that thread.
    """

    def __init__(self) -> None:
        """Start with no subscribers and no bound loop; the first :meth:`add` adopts the loop."""
        # Subscribers per signal, in registration order; dispatch walks them
        # backwards. A signal with no subscribers carries no entry, so the dict
        # being empty is exactly "this chain owns no asyncio handler".
        self._subs: dict[signal.Signals, list[SignalSubscription]] = {}
        # The loop the installed handlers belong to. Handlers are loop-owned, so
        # a chain spanning two live loops would deliver to the wrong one.
        self._loop: asyncio.AbstractEventLoop | None = None
        # The signals handed back to their default dispositions by the open
        # :meth:`released` span, or ``None`` while no span is open.
        self._released: tuple[signal.Signals, ...] | None = None

    def add(self, sig: signal.Signals, callback: Callable[[], object], *, name: str) -> SignalSubscription:
        """Subscribe ``callback`` to ``sig`` and return the handle that leaves again.

        ``callback`` takes no arguments and its return value is ignored (a bound
        ``Task.cancel`` is a subscriber); one that wants the signal binds it with
        ``functools.partial``. ``name`` identifies the subscriber in the
        report a raising callback produces.

        Raises whatever the underlying install raises (``RuntimeError`` off the
        main thread or with no running loop, ``ValueError`` for a signal that
        cannot be handled, ``NotImplementedError`` off POSIX): a component that
        believes it is signal-wired and is not must find out here.
        """
        loop = asyncio.get_running_loop()
        self._adopt(loop)
        subs = self._subs.setdefault(sig, [])
        if not subs:
            try:
                loop.add_signal_handler(sig, self._dispatch, sig)
            except BaseException:
                # A refused install must leave NO trace: an empty entry would read
                # as "this signal is already owned" and make the next add skip the
                # install it still needs.
                del self._subs[sig]
                raise
        self._loop = loop
        sub = SignalSubscription(self, sig, callback, name)
        subs.append(sub)
        return sub

    def subscribers(self, sig: signal.Signals) -> tuple[str, ...]:
        """The names currently subscribed to ``sig``, in registration order."""
        return tuple(sub.name for sub in self._subs.get(sig, ()))

    @contextmanager
    def released(self, *sigs: signal.Signals) -> Iterator[None]:
        """Hand each of ``sigs`` the chain owns back to its default disposition for the span.

        For a body that blocks the loop thread: no subscriber can run until it returns,
        so a signal kept on the chain would be deferred for the body's whole span. On
        entry the chain's handler is uninstalled for each of ``sigs`` that has
        subscribers (``SIGINT`` gets ``default_int_handler`` back, any other signal
        ``SIG_DFL``); a signal without subscribers is left as it is. On exit the handler
        is installed again for each of those signals that still has subscribers. The
        subscriber table is not touched.

        One span at a time: a nested span raises ``RuntimeError``. A refused reinstall
        propagates, as a refused install does in :meth:`add`.
        """
        if self._released is not None:
            raise RuntimeError(
                f"signal chain is already released for {', '.join(sig.name for sig in self._released)}; "
                "one release span may be open at a time"
            )
        # A chain bound to no loop has no subscribers, so it owns none of ``sigs``.
        loop = self._loop
        held = tuple(sig for sig in sigs if sig in self._subs) if loop is not None else ()
        self._released = held
        try:
            if loop is not None:
                for sig in held:
                    loop.remove_signal_handler(sig)
            yield
        finally:
            self._released = None
            if loop is not None:
                for sig in held:
                    if sig in self._subs:
                        loop.add_signal_handler(sig, self._dispatch, sig)

    def _adopt(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind this chain to ``loop``, refusing a second LIVE loop.

        Two running loops in one process would each need their own handler for
        the same signal, which is the very collision this chain exists to
        prevent — so that is a loud error. A bound loop that is no longer running
        is a different case: its handlers can never fire again and its
        subscribers can never be reached, so the stale table is dropped and the
        new loop starts clean.
        """
        if self._loop is None or self._loop is loop:
            return
        if self._loop.is_running():
            raise RuntimeError(
                f"signal chain is already bound to a different running event loop ({self._loop!r}); "
                "one process may compose signals on one loop only"
            )
        self._subs.clear()
        self._loop = None

    def _dispatch(self, sig: signal.Signals) -> None:
        """Deliver ``sig`` to every subscriber, innermost first.

        Walks a SNAPSHOT so a subscriber that cancels (or subscribes) from inside
        its own callback cannot corrupt the walk, and isolates each callback: one
        raising subscriber must never swallow the rest — above all it must never
        stop the launcher's main-task cancellation, which is what guarantees
        teardown.
        """
        for sub in reversed(tuple(self._subs.get(sig, ()))):
            try:
                sub.callback()
            except Exception:
                logger.error("signal-chain subscriber %r raised on %s", sub.name, sig.name, exc_info=True)

    def _remove(self, sub: SignalSubscription) -> None:
        """Drop one subscriber, uninstalling the asyncio handler only when it was the last for its signal.

        An earlier uninstall would restore SIGTERM to ``SIG_DFL`` under the
        subscribers that stayed.
        """
        subs = self._subs.get(sub.sig)
        if subs is None or sub not in subs:
            return
        subs.remove(sub)
        if subs:
            return
        del self._subs[sub.sig]
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.remove_signal_handler(sub.sig)
        if not self._subs:
            self._loop = None


# The one process-wide chain. Every component that wants a signal — the CLI's
# main-task cancellation, a backend runtime's drain request — subscribes here.
signal_chain = SignalChain()
