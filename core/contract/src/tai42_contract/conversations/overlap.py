"""The per-route overlap policy and the supersede signal for turns that hand over to a newer message."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The inclusive upper bound, in seconds, on a route's settle window.
SETTLE_SECONDS_MAX = 30


class OverlapPolicy(BaseModel):
    """How a route treats a running turn and the newer participant messages that overlap it.

    Governs participant MESSAGE turns on one thread only (``origin="client"``,
    ``inbound_kind="message"``); an event turn always runs as its own turn. Two knobs and a
    window:

    * ``running`` — what happens to the turn in flight when a newer message is accepted:
      ``continue`` leaves it running (one turn per message), ``cancel`` cancels it cooperatively
      in favour of the newer message.
    * ``deliver`` — what a turn carries: ``one`` exactly one message per turn, ``all`` every
      message accepted since the last turn started, in order, as one turn.
    * ``settle_seconds`` — a turn starts no earlier than this many seconds after its lead
      message was accepted, so a burst inside the window rides in one turn. ``0`` disables it.

    The default policy (``continue``/``one``/``0``) runs one turn per message and leaves the
    payload unchanged. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    running: Literal["continue", "cancel"] = "continue"
    deliver: Literal["one", "all"] = "one"
    settle_seconds: int = Field(
        default=0,
        ge=0,
        le=SETTLE_SECONDS_MAX,
        description=(
            "Seconds a turn waits after its lead message was accepted before it starts, so a "
            "burst inside the window rides one turn; 0 disables the window."
        ),
    )

    @model_validator(mode="after")
    def _settle_requires_batching_or_cancel(self) -> OverlapPolicy:
        """A settle window needs a reason to delay a turn: batching (``all``) or cancellation.

        With ``continue`` + ``one`` a window would delay every turn while still running each
        message as its own turn — a pure delay for nothing — so it is refused loudly rather than
        accepted as a silent no-op.
        """
        if self.settle_seconds > 0 and self.deliver != "all" and self.running != "cancel":
            raise ValueError(
                'settle_seconds > 0 requires deliver="all" or running="cancel"; '
                "a settle window under continue+one delays every turn for nothing"
            )
        return self


class TurnSupersededError(BaseException):
    """A running turn hands over to a newer message: resolve it ``superseded``, no reply, no delivery.

    Raised by the platform when its cancel watcher translates the owner task's cancellation, and
    by a target that yields cooperatively (a body that reads the pending seam and stops before an
    irreversible step). ``successor_id`` is the ``message_id`` of the turn that takes this turn's
    place; the platform stamps it onto the superseded record.

    Derives from :class:`BaseException`, not :class:`Exception`, so that no ``except Exception``
    between the yielding body and the two arms that resolve the turn (the tool-turn arm and the
    scheduled-task arm) can swallow the hand-over and mistake it for a turn error or feed it to a
    retry loop — the same discipline :class:`asyncio.CancelledError` follows. Only the two arms,
    which catch it by name, resolve it.
    """

    def __init__(self, successor_id: str) -> None:
        """Build the signal, recording ``successor_id`` — the message_id of the succeeding turn."""
        super().__init__(f"turn superseded by message {successor_id!r}")
        self.successor_id = successor_id
