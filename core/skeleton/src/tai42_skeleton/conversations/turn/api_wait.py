"""The api-door sync-wait / async-callback split and its :class:`ApiSubmitResult` DTO."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tai42_contract.conversations import ConversationAnswer

from tai42_skeleton.conversations.delivery import mark_wait_delivered
from tai42_skeleton.conversations.models import ConversationRecord
from tai42_skeleton.conversations.turn.schedule import _spawn_delivery_on_success


@dataclass(frozen=True)
class ApiSubmitResult:
    """The outcome the API door turns into its HTTP response.

    ``answer`` is set only when the bounded sync-wait finished the turn in time (→ ``200``);
    otherwise the turn is still running behind the callback (→ ``202``).
    """

    message_id: str
    thread_id: str
    answer: ConversationAnswer | None


async def _api_wait_or_callback(
    task: asyncio.Task[ConversationRecord],
    message_id: str,
    thread_id: str,
    wait_seconds: int,
    client_connected: Callable[[], Awaitable[bool]],
) -> ApiSubmitResult:
    """The api door's sync-wait / async-callback split, shared by every api-door turn.

    Shared by the message door and an event's turn on an api-door thread. Within
    ``wait_seconds`` a turn that finished — an answer or an explicit silent marker — is returned
    inline in the ``200`` and its callback suppressed, but ONLY while ``client_connected`` reports
    a receiver: the inline answer's precondition is a live client to write it to. The probe is
    awaited BEFORE the delivery claim (a claim then un-claim has no atomic reversal). A gone
    client, or a lost claim to a racing delivery, falls through to ``_deliver_when_done`` so
    exactly one of the wait path and the callback delivers, and the ``202`` shape is returned —
    the answer then reaches a hung-up caller by the route's callback (or its terminal poll
    record). ``wait_seconds == 0`` never evaluates the probe.
    """
    if wait_seconds > 0:
        done, _pending = await asyncio.wait({task}, timeout=wait_seconds)
        if task in done and task.exception() is None:
            record = task.result()
            if await client_connected() and await mark_wait_delivered(message_id):
                return ApiSubmitResult(message_id=message_id, thread_id=thread_id, answer=record.answer_payload())
            # Client gone before the claim, or lost the claim to a racing delivery — fall through
            # to the async shape.
    _deliver_when_done(task, message_id)
    return ApiSubmitResult(message_id=message_id, thread_id=thread_id, answer=None)


def _deliver_when_done(task: asyncio.Task[ConversationRecord], message_id: str) -> None:
    """Spawn the record's delivery when its turn task completes successfully."""
    if task.done():
        _spawn_delivery_on_success(task, message_id)
        return
    task.add_done_callback(lambda t: _spawn_delivery_on_success(t, message_id))
