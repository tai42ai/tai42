"""The operator-send door: an operator's message into a thread, minted already answered."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from tai42_contract.channels import ChannelTemplate, Option, OptionSection
from tai42_contract.conversations import AnswerPart, ConversationRoute
from tai42_contract.interactions import LocationElement, MediaItem

from tai42_skeleton.conversations.caps import get_turn_caps
from tai42_skeleton.conversations.delivery import spawn_delivery
from tai42_skeleton.conversations.mode import supports_thread_append
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn.errors import OperatorAppendError
from tai42_skeleton.conversations.turn.record import _answer_fields, _new_record


async def operator_send(
    *,
    route: ConversationRoute,
    thread_id: str,
    client_address: str,
    text: str,
    operator_principal: str,
    media: list[MediaItem] | None = None,
    template: ChannelTemplate | None = None,
    options: list[Option] | None = None,
    location: LocationElement | None = None,
    sections: list[OptionSection] | None = None,
    header: MediaItem | None = None,
    footer: str | None = None,
    schema: dict[str, Any] | None = None,
) -> str:
    """Send an operator's message ``text`` into ``thread_id`` on ``route``, returning its ``message_id``.

    The returned ``message_id`` is a uuid4. No turn runs: the record is minted already
    ``answered`` carrying the operator's text and handed to the delivery machine, which sends
    it from the route identity exactly as it sends a produced answer (same chunking, ledger
    and receipts). Allowed in either mode; it never flips the mode.

    ``media``, ``template``, ``options``, ``location``, ``sections``, ``header``, ``footer``
    and ``schema`` (an ask-less form's answer schema —
    the participant's submission enters the conversation as an ordinary inbound message) are
    OPTIONAL richer-send forms — FULL parity with the flow answer path's :class:`AnswerPart`
    vocabulary: when any is set
    the reply is stored as a single rich :class:`AnswerPart` (``message=text`` carrying the
    rich fields), so the delivery machine sends the operator's message with
    its
    rich fields exactly as it sends a produced rich part — including the capability gate: a
    channel that does not advertise the matching ``supports_*_notifications`` flag never
    receives the part, and the record fails loudly instead of the field silently dropping.
    With none set the record stays a plain single-message answer (byte-parity with the
    pre-rich operator send). A contract-invalid value (an empty list/dict, an over-cap value,
    or a combination the shared composition matrix refuses — options XOR sections, schema
    excludes both, header/footer require a choice surface, template standalone) raises before
    any state is
    written.

    For an agent target that HOLDS thread memory (implements ``append_thread_messages``) the
    text is appended to the thread's checkpoint as an ``assistant`` message BEFORE the record
    is created, mirroring how an agent's own answer enters its memory, so a later agent turn
    reads the operator's reply as prior context; a memoryless agent target (leaves the ABC
    default), an unregistered agent and a tool target have no thread memory to feed, so nothing
    is appended. The order is resolve → append → create + spawn: an append that fails raises
    :class:`OperatorAppendError` and no record is created, and a create that fails after the
    append also raises — leaving a duplicated memory line a retry would add again, which is
    accepted over a phantom record with no memory behind it.

    The whole append → create → spawn runs under the thread's per-thread FIFO
    (:meth:`TurnCaps.run_reserved`), the same lock in-flight turns take, so the operator's
    write never interleaves a turn's checkpoint and record writes; the HTTP call waits behind
    an in-flight turn. ``run_reserved`` holds the cross-worker thread lease for its span, so an
    operator send and an in-flight turn on the same thread serialize across workers too and
    never fork its checkpoint. As a live-caller sync door the acquisition is bounded by
    ``sync_door_wait_seconds``: a wait past it — behind a turn possibly HITL-paused on another
    worker — raises the loud, retriable :class:`ThreadBusyError` (503) rather than blocking the
    caller past the proxy timeout. A full FIFO raises the loud, retriable
    :class:`ThreadQueueOverflowError` (503) before anything is written.
    """
    # A rich operator send (media/template/options/schema present) stores one
    # :class:`AnswerPart`
    # carrying the text plus its rich fields — the shape the delivery machine sends as a rich
    # part. A plain send keeps ``answer=text`` with no parts, byte-identical to the pre-rich
    # path (and unbounded by the part message cap, which only governs a rich part's text).
    if (
        media is None
        and template is None
        and options is None
        and location is None
        and sections is None
        and header is None
        and footer is None
        and schema is None
    ):
        answer: str = text
        answer_parts: list[AnswerPart] | None = None
    else:
        answer, answer_parts = _answer_fields(
            [
                AnswerPart(
                    message=text,
                    media=media,
                    template=template,
                    options=options,
                    location=location,
                    sections=sections,
                    header=header,
                    footer=footer,
                    schema=schema,
                )
            ]
        )
    message_id = str(uuid4())
    caps = get_turn_caps()
    caps.reserve_thread_slot(thread_id)
    async with caps.run_reserved(thread_id, acquire_timeout_seconds=caps.settings.sync_door_wait_seconds):
        if route.target_kind == "agent":
            agent = accessors._agent_registry().get(route.target_name)
            if agent is not None and supports_thread_append(agent):
                try:
                    await agent.append_thread_messages(
                        thread_id=thread_id, messages=[{"role": "assistant", "content": text}]
                    )
                except Exception as exc:
                    raise OperatorAppendError(
                        f"appending the operator message to thread {thread_id!r} on route {route.route_name!r} "
                        f"failed: {exc}"
                    ) from exc
        record = _new_record(
            route=route,
            message_id=message_id,
            thread_id=thread_id,
            client_address=client_address,
            caller_principal=operator_principal,
            provider_message_id=None,
            inbound_text="",
            delivery_status=DeliveryStatus.PENDING_DELIVERY,
            answer_status="answered",
            answer=answer,
            answer_parts=answer_parts,
            origin="operator",
            operator_send=True,
        )
        await accessors._store().create_record(record)
        await accessors._refresh_thread_mode_ttl(thread_id)
        spawn_delivery(message_id)
    return message_id
