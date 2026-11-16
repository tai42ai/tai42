"""The working-on-it (typing) indicator shown to the user a run owes a reply.

A channel that advertises a vendor working-on-it indicator (a ``ClassVar
working_signal_expiry_seconds``) has that indicator refreshed while a run owes its user a reply.
One loop runs per run id, in one detached task:

* a turn's loop starts at the schedule seam (:func:`start_turn`) and asserts from accept; the
  turn ends it with :func:`stop`;
* a resumed run's loop lives while a drive holds it (:func:`hold`); it asserts nothing until no
  question the run sent stands, whoever it went to: a drive states it through :func:`resumed`, or
  the close of the last question standing leaves none;
* the reply's send ends either loop with :func:`stop`.

The loop pauses while a question the run waits on stands, whoever answers it and however it was
delivered (:func:`question_sent`, cleared by :func:`question_answered`, :func:`question_withdrawn`
or :func:`resumed`), and never ends for it. Only an answer from the loop's own user lends the
answering message's id to the indicator.
One stretch of showing the indicator runs at most the conversations ceiling; at the ceiling the
indicator is cleared and the loop waits, dormant, for the next stretch. Every public call is a
synchronous enqueue the loop task applies in arrival order once it knows its user, and never
raises to its caller: the indicator is a cosmetic, so a fault is logged and the run is
unaffected. The loop ends on its own when its record leaves the in-flight delivery states (read
before a stretch's first assert and at every refresh; a cross-process send), when a read it
needs fails, and after three asserts in a row fail. Every channel's indicator lapses on its own
within its advertised lifetime, so a clear that never lands leaves it shown no longer than that.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, Literal

from tai42_contract.interactions import get_park_completion
from tai42_contract.tools import get_run_delivery_id

from tai42_skeleton.app.root_task import spawn_root_task
from tai42_skeleton.conversations import cache
from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.settings import conversations_settings
from tai42_skeleton.interactions.run_completion import run_completion_id

logger = logging.getLogger(__name__)

# The user a loop shows the indicator to: ``(channel, our identity, user address)``, the triple a
# send record carries.
_Target = tuple[str, str | None, str]

# While the record sits in one of these the reply has not gone out, so the indicator is
# still worth refreshing; any other status means the send happened or never will.
_IN_FLIGHT_STATUSES = frozenset({DeliveryStatus.ACCEPTED, DeliveryStatus.PENDING_DELIVERY})

# Consecutive vendor failures after which the loop stops hammering a persistent fault.
_MAX_CONSECUTIVE_FAILURES = 3


@dataclass(frozen=True)
class _Release:
    """The last holder let go."""


@dataclass(frozen=True)
class _Resumed:
    interaction_id: str
    medium_message_id: str | None
    pending: tuple[str, ...]


@dataclass(frozen=True)
class _QuestionSent:
    interaction_id: str


@dataclass(frozen=True)
class _QuestionClosed:
    """A question no longer stands: answered (with the answering message's id, if any) or withdrawn."""

    interaction_id: str
    medium_message_id: str | None


_Event = _Release | _Resumed | _QuestionSent | _QuestionClosed


class _LoopEndError(Exception):
    """The loop must end now; raised inside the task only."""


def _now() -> float:
    """Monotonic seconds, the clock of the stretch deadlines and the refresh grid."""
    return time.monotonic()


def _signal_channel(channel_name: str, run_id: str) -> tuple[Any, float] | None:
    """The channel object and its indicator lifetime, or ``None`` when nothing should signal.

    ``None`` when the channel is not registered or advertises no vendor indicator; a non-positive
    lifetime (a plugin misconfiguration) is warned about.
    """
    from tai42_skeleton.app import instance

    try:
        channel = instance.app.channels.get(channel_name)
    except KeyError:
        logger.debug("conversations: channel %s is not registered; no working signal for %s", channel_name, run_id)
        return None
    expiry = getattr(channel, "working_signal_expiry_seconds", None)
    if expiry is None:
        return None
    if expiry <= 0:
        logger.warning(
            "conversations: channel %s advertises a non-positive working_signal_expiry_seconds "
            "(%r); no working signal for %s",
            channel_name,
            expiry,
            run_id,
        )
        return None
    return channel, expiry


class _RunSignal:
    """One run's loop: who holds it, the user it signals, the questions the run has standing."""

    def __init__(self, run_id: str, *, thread_id: str | None, holds: int) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.holds = holds
        self.target: _Target | None = None
        self.inbound_message_id: str | None = None
        # The run's standing questions, whoever each went to.
        self.standing: set[str] = set()
        # ``None`` while dormant or paused; else the end of the running stretch.
        self.stretch_deadline: float | None = None
        self.active_sent = False
        self.events: deque[_Event] = deque()
        self.wake = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self._channel: Any = None
        self._interval = 0.0
        self._ceiling = 0.0
        self._anchor = 0.0
        self._slot = 0
        self._next_tick = 0.0
        self._assert_now = False
        self._failures = 0

    # -- the calls the public functions make -------------------------------------------------

    def post(self, event: _Event) -> None:
        self.events.append(event)
        self.wake.set()

    def release(self) -> None:
        self.holds -= 1
        if self.holds <= 0:
            self.post(_Release())

    def spawn(self) -> None:
        self.task = spawn_root_task(self._run(), name=f"working-signal-{self.run_id}")
        self.task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("conversations: the working-signal loop for %s crashed", self.run_id, exc_info=exc)

    # -- the task ----------------------------------------------------------------------------

    def open_turn(self, record: ConversationRecord, channel: Any, expiry: float) -> None:
        """Know the turn's user at once and open its first stretch from accept."""
        assert record.channel is not None  # noqa: S101 (start_turn starts no loop without a channel)
        self.target = (record.channel, record.our_identity, record.client_address)
        self.inbound_message_id = record.provider_message_id
        self._configure(channel, expiry)
        self._open_stretch()

    def _configure(self, channel: Any, expiry: float) -> None:
        settings = conversations_settings()
        self._channel = channel
        self._interval = max(expiry - settings.working_signal_refresh_margin_seconds, expiry / 2)
        self._ceiling = settings.working_signal_max_seconds
        self._anchor = _now()
        self._slot = 0
        self._next_tick = self._anchor + self._interval

    async def _resolve_run_target(self) -> bool:
        """Resolve a resumed run's user from its thread; ``False`` when the loop must end."""
        from tai42_skeleton.conversations.turn.completion_delivery import resolve_completion_target

        assert self.thread_id is not None  # noqa: S101 (a run loop is always created with its thread)
        try:
            route, client_address = await resolve_completion_target(self.thread_id)
            identity = None if route.our_identity is None else canonical_address(route.our_identity)
            address = canonical_address(client_address)
        except Exception:
            logger.error(
                "conversations: resolving the user of thread %s for the working signal of %s failed; "
                "no working signal for it",
                self.thread_id,
                self.run_id,
                exc_info=True,
            )
            return False
        if route.channel is None:
            return False
        found = _signal_channel(route.channel, self.run_id)
        if found is None:
            return False
        self.target = (route.channel, identity, address)
        self._configure(*found)
        return True

    async def _run(self) -> None:
        try:
            if self.target is None and not await self._resolve_run_target():
                return
            await self._serve()
        except _LoopEndError:
            return
        finally:
            # Leaving the registry comes before any await, so a hold arriving from here on starts
            # a new loop instead of joining this one.
            if _SIGNALS.get(self.run_id) is self:
                del _SIGNALS[self.run_id]
            await self._clear()

    async def _serve(self) -> None:
        await self._drain()
        while True:
            if self._assert_now:
                self._assert_now = False
                await self._check_record()
                await self._assert()
                self._advance_grid()
            kind, at = self._next_wake()
            timed_out = await self._wait(at - _now())
            await self._drain()
            if not timed_out or self._assert_now:
                continue
            if kind == "deadline" or (self.stretch_deadline is not None and _now() >= self.stretch_deadline):
                # The stretch is spent (or a starved sleep overran it): nothing is sent past it.
                await self._close_stretch()
                if kind == "deadline":
                    continue
            await self._check_record()
            if self.stretch_deadline is not None:
                await self._assert()
            self._advance_grid()

    def _next_wake(self) -> tuple[Literal["deadline", "tick"], float]:
        if self.stretch_deadline is not None and self._next_tick >= self.stretch_deadline:
            # No refresh is due before the ceiling: hold the indicator until it.
            return "deadline", self.stretch_deadline
        return "tick", self._next_tick

    def _advance_grid(self) -> None:
        # Ticks fall on a fixed grid, ``anchor + n * interval``, and a tick is served when the sleep
        # aimed at it ends, never decided on the instant the sleep happened to end: the event loop's
        # timers fire on their own grid (uvloop rounds every delay to the millisecond), so a sleep can
        # end a fraction of a millisecond early on this clock. The next slot is the one after the slot
        # just served, or the first one still ahead when a slow cycle overran some (those are
        # skipped, never sent back to back).
        self._slot = max(self._slot + 1, math.floor((_now() - self._anchor) / self._interval) + 1)
        self._next_tick = self._anchor + self._slot * self._interval

    async def _wait(self, delay: float) -> bool:
        """Sleep ``delay`` or until an event arrives; ``True`` when the sleep ran out."""
        if self.events:
            return False
        self.wake.clear()
        sleeper = asyncio.ensure_future(asyncio.sleep(max(delay, 0.0)))
        waker = asyncio.ensure_future(self.wake.wait())
        try:
            done, _ = await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waker.cancel()
        return sleeper in done

    # -- applying the events -------------------------------------------------------------------

    async def _drain(self) -> None:
        while self.events:
            event = self.events.popleft()
            if isinstance(event, _Release):
                if self.holds <= 0:
                    raise _LoopEndError
            elif isinstance(event, _Resumed):
                await self._apply_resumed(event)
            elif isinstance(event, _QuestionSent):
                # The run now waits on whoever answers the question, so nothing is shown meanwhile.
                self.standing.add(event.interaction_id)
                await self._pause()
            else:
                if event.interaction_id not in self.standing:
                    continue
                self.standing.discard(event.interaction_id)
                await self._adopt_answer(event.interaction_id, event.medium_message_id)
                if not self.standing and self.stretch_deadline is None:
                    self._open_stretch()

    async def _apply_resumed(self, event: _Resumed) -> None:
        self.standing.discard(event.interaction_id)
        self.standing.update(event.pending)
        await self._adopt_answer(event.interaction_id, event.medium_message_id)
        if self.standing:
            await self._pause()
            return
        if self.stretch_deadline is None:
            self._open_stretch()

    async def _adopt_answer(self, interaction_id: str, medium_message_id: str | None) -> None:
        """Make the answering message the one the indicator refers to, when the loop's user sent it."""
        if medium_message_id is not None and await self._asked_on_target(interaction_id):
            self.inbound_message_id = medium_message_id

    async def _asked_on_target(self, interaction_id: str) -> bool:
        """Whether the ask ``interaction_id`` went to this loop's user, read off its send record."""
        try:
            records = cache.get_conversations_manager().records
            message_id = await records.ask_send_message_id(interaction_id)
            record = None if message_id is None else await records.get_record(message_id)
        except Exception:
            logger.error(
                "conversations: reading the send record of ask %s for the working signal of %s failed; "
                "stopping the signal",
                interaction_id,
                self.run_id,
                exc_info=True,
            )
            raise _LoopEndError from None
        if record is None:
            return False
        return (record.channel, record.our_identity, record.client_address) == self.target

    def _open_stretch(self) -> None:
        now = _now()
        self.stretch_deadline = now + self._ceiling
        self._anchor = now
        self._slot = 0
        self._next_tick = now
        self._assert_now = True

    async def _pause(self) -> None:
        self.stretch_deadline = None
        self._assert_now = False
        await self._clear()

    async def _close_stretch(self) -> None:
        self.stretch_deadline = None
        await self._clear()

    # -- the record backstop and the vendor calls ---------------------------------------------

    async def _check_record(self) -> None:
        try:
            record = await cache.get_conversations_manager().records.get_record(self.run_id)
        except Exception:
            logger.warning(
                "conversations: reading record %s to refresh its working signal failed; stopping the signal",
                self.run_id,
                exc_info=True,
            )
            raise _LoopEndError from None
        if record is not None and record.delivery_status not in _IN_FLIGHT_STATUSES:
            raise _LoopEndError

    async def _assert(self) -> None:
        """Assert the indicator once; three failures in a row end the loop.

        A vendor fault (``ChannelDeliveryError`` or any other) is logged at WARNING and counted. An
        assert counts as shown from the moment it starts, so a cancellation landing while it is in
        flight still leaves the clear owed.
        """
        assert self.target is not None  # noqa: S101 (asserts run only once the user is known)
        shown_before = self.active_sent
        self.active_sent = True
        try:
            await self._channel.signal_working(
                recipient=self.target[2],
                sender_identity=self.target[1],
                provider_message_id=self.inbound_message_id,
                active=True,
            )
        except Exception:
            logger.warning(
                "conversations: refreshing the working signal for %s on channel %s failed",
                self.run_id,
                self.target[0],
                exc_info=True,
            )
            self.active_sent = shown_before
            self._failures += 1
            if self._failures >= _MAX_CONSECUTIVE_FAILURES:
                raise _LoopEndError from None
            return
        self._failures = 0

    async def _clear(self) -> None:
        """Clear the indicator once, only when an assert started since the last clear began.

        A vendor fault on the clear is logged, never raised.
        """
        if not self.active_sent or self.target is None:
            return
        self.active_sent = False
        try:
            await self._channel.signal_working(
                recipient=self.target[2],
                sender_identity=self.target[1],
                provider_message_id=self.inbound_message_id,
                active=False,
            )
        except Exception:
            logger.warning(
                "conversations: clearing the working signal for %s on channel %s failed",
                self.run_id,
                self.target[0],
                exc_info=True,
            )


# The live loop per run id; a loop leaves at the start of its task's teardown, or when ``stop``
# or a replacing ``start_turn`` takes it out, so a loop that ended never evicts its replacement.
_SIGNALS: dict[str, _RunSignal] = {}


def start_turn(record: ConversationRecord) -> None:
    """Start the loop for ``record``'s turn, unless nothing should signal.

    Starts nothing when the loop is disabled (the ceiling is 0), the record has no channel (the api
    door), the channel is not registered or advertises no vendor indicator, or the advertised
    lifetime is non-positive. A second start for a ``message_id`` already looping stops the earlier
    loop and replaces it.
    """
    try:
        if conversations_settings().working_signal_max_seconds <= 0:
            return
        if record.channel is None:
            return
        found = _signal_channel(record.channel, record.message_id)
        if found is None:
            return
        if record.message_id in _SIGNALS:
            logger.debug(
                "conversations: a working-signal loop for record %s is already running; replacing it",
                record.message_id,
            )
            stop(record.message_id)
        signal = _RunSignal(record.message_id, thread_id=record.thread_id, holds=0)
        signal.open_turn(record, *found)
        signal.spawn()
        _SIGNALS[record.message_id] = signal
    except Exception:
        logger.exception(
            "conversations: starting the working-signal loop for record %s failed; the turn is unaffected",
            record.message_id,
        )


def hold(run_id: str, *, thread_id: str) -> AbstractContextManager[None]:
    """Hold ``run_id``'s loop for the ``with`` body, creating it (paused, user unknown) or joining it.

    The loop ends at the last holder's release unless it was stopped first; a release is bound to
    the loop it joined, so a holder never ends a loop that replaced it.
    """
    try:
        if conversations_settings().working_signal_max_seconds <= 0:
            return nullcontext()
        signal = _SIGNALS.get(run_id)
        if signal is None:
            signal = _RunSignal(run_id, thread_id=thread_id, holds=1)
            signal.spawn()
            _SIGNALS[run_id] = signal
        else:
            signal.holds += 1
    except Exception:
        logger.exception("conversations: holding the working-signal loop for %s failed; the run is unaffected", run_id)
        return nullcontext()
    return _held(signal)


@contextmanager
def _held(signal: _RunSignal) -> Iterator[None]:
    try:
        yield
    finally:
        signal.release()


def resumed(run_id: str, interaction_id: str, *, medium_message_id: str | None, pending: Sequence[str]) -> None:
    """A drive claimed ``interaction_id``'s answer; ``pending`` are the run's questions still open.

    Any of ``pending`` keeps it paused, whoever it went to; with none standing a new stretch opens,
    showing the indicator again. The answering message's ``medium_message_id`` becomes the id the
    indicator refers to when the answered question went to the loop's user.
    """
    signal = _SIGNALS.get(run_id)
    if signal is not None:
        signal.post(_Resumed(interaction_id, medium_message_id, tuple(pending)))


def question_sent(run_id: str | None, interaction_id: str) -> None:
    """The run now waits on the question ``interaction_id``; whoever answers it, it pauses the loop."""
    signal = None if run_id is None else _SIGNALS.get(run_id)
    if signal is not None:
        signal.post(_QuestionSent(interaction_id))


def question_answered(run_id: str | None, interaction_id: str, medium_message_id: str | None) -> None:
    """The question ``interaction_id`` was answered by the message ``medium_message_id``."""
    signal = None if run_id is None else _SIGNALS.get(run_id)
    if signal is not None:
        signal.post(_QuestionClosed(interaction_id, medium_message_id))


def question_withdrawn(run_id: str | None, interaction_id: str) -> None:
    """The question ``interaction_id`` no longer stands: the ask that sent it raised.

    A refused delivery, a failed or timed-out wait, a failed read of the answer's quote and a
    cancel all end it this way.
    """
    signal = None if run_id is None else _SIGNALS.get(run_id)
    if signal is not None:
        signal.post(_QuestionClosed(interaction_id, None))


def stop(run_id: str) -> None:
    """End ``run_id``'s loop whatever holds it; its task clears the indicator once if it was shown.

    The loop leaves the registry at once, so a later hold starts a new loop. A ``run_id`` with no
    live loop is a no-op (it already ended, or it runs in another process).
    """
    signal = _SIGNALS.pop(run_id, None)
    if signal is not None and signal.task is not None:
        signal.task.cancel()


def current_run_id() -> str | None:
    """The id of the reply the running work owes its user, or ``None`` when it owes none.

    A conversation turn binds one of the conversation completion tools on the park completion,
    its context carrying the turn record's ``message_id``: the turn's reply is that record. Any
    other run with a delivery identity owes the reply its completion record carries, keyed by
    :func:`run_completion_id`.
    """
    from tai42_skeleton.conversations.turn.completion_delivery import (
        COMPLETION_TOOL_NAME,
        DELIVER_TOOL_COMPLETION_NAME,
    )

    completion = get_park_completion()
    if completion.tool in (COMPLETION_TOOL_NAME, DELIVER_TOOL_COMPLETION_NAME):
        message_id = (completion.context or {}).get("message_id")
        if isinstance(message_id, str):
            return message_id
    run_delivery_id = get_run_delivery_id()
    return None if run_delivery_id is None else run_completion_id(run_delivery_id)


def has_loop(run_id: str | None) -> bool:
    """Whether a live loop runs for ``run_id`` in this process."""
    return run_id is not None and run_id in _SIGNALS
