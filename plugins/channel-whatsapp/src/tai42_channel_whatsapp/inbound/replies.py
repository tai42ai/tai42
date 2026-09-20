"""Reply-capable content handlers: text, interactive tap, and template button.

Each resolves a pending question via the shared answer ladder or routes the
message to the conversation bridge.
"""

from __future__ import annotations

from typing import Any

from tai42_channel_whatsapp.correlation import PendingQuestion, already_seen, peek_pending
from tai42_channel_whatsapp.inbound.answers import _bridge_inbound, _resolve_answer
from tai42_channel_whatsapp.inbound.forms import _handle_form_reply
from tai42_channel_whatsapp.inbound.params import _merged_params, _put_param


async def _handle_text(
    message: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str, params: dict[str, str]
) -> None:
    """A typed reply: resolve it against a pending question via the shared ladder, else route to the bridge.

    ``params`` are the message-level referral/reply-context entries, carried onto the bridged
    turn — including the expired-ask fallback bridge (the correlated forward itself takes none).
    """
    if await already_seen(wamid):
        return
    text_field = message.get("text")
    body = text_field.get("body") if isinstance(text_field, dict) else None
    text = body if isinstance(body, str) else ""

    pending = await peek_pending(phone_number_id, wa_id)
    if pending is None:
        # No pending question (unrelated text or expired) — route to the bridge.
        await _bridge_inbound(phone_number_id, wa_id, text, wamid, params=params or None)
        return
    # A typed reply answers with its body minus outer whitespace.
    await _resolve_answer(phone_number_id, wa_id, wamid, text.strip(), pending, params=params or None)


def _extract_interactive_reply(interactive: Any) -> tuple[str | None, str, str | None]:
    """The tapped ``(id, title, description)`` from an interactive reply, or ``(None, "", None)``.

    ``id`` is None when the button/list reply is missing or malformed; ``title`` is the
    human-readable label bridged when the tap is not an answer; ``description`` is a
    ``list_reply``'s optional secondary line (``None`` for a button reply, which has none).
    """
    if not isinstance(interactive, dict):
        return None, "", None
    reply_type = interactive.get("type")
    reply = interactive.get(reply_type) if isinstance(reply_type, str) else None
    if reply_type not in ("button_reply", "list_reply") or not isinstance(reply, dict):
        return None, "", None
    reply_id = reply.get("id")
    title = reply.get("title")
    description = reply.get("description")
    return (
        (reply_id if isinstance(reply_id, str) else None),
        (title if isinstance(title, str) else ""),
        (description if isinstance(description, str) else None),
    )


def _map_tap_to_answer(reply_id: str | None, pending: PendingQuestion) -> str | None:
    """The option text a tap answers, or ``None`` when the tap is NOT an answer.

    A tap answers only when its id's interaction part EQUALS the pending ask's and
    its index is in range for that ask's options. A malformed id, an out-of-range
    index, a mismatched interaction part (a stale button from an earlier ask), or a
    pending question with no options (a text ask) all yield ``None`` — the caller
    restores the pending question and bridges the tap's title instead.
    """
    if reply_id is None or pending.options is None or pending.interaction_id is None:
        return None
    interaction_part, separator, index_part = reply_id.rpartition(":")
    if not separator or interaction_part != pending.interaction_id or not index_part.isdigit():
        return None
    try:
        index = int(index_part)
    except ValueError:
        # ``isdigit()`` is True for Unicode digit characters (e.g. "²") and for
        # absurdly-long digit strings, both of which ``int()`` rejects — a
        # non-answer, never a propagating 5xx that has Meta redeliver forever.
        return None
    if index >= len(pending.options):
        return None
    return pending.options[index]


async def _handle_interactive(
    message: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str, params: dict[str, str]
) -> None:
    """A button tap or list pick: map it to a pending ask's option, else bridge the tap's title.

    A tap whose id matches the pending ask answers it (``options[index]``). A tap
    with no pending question, or one whose id is stale/malformed/out-of-range, is
    NOT an answer: the pending question is left untouched and the tap's title goes
    to the bridge like any unrelated message — carrying the tapped ``reply_id`` (and a
    ``list_reply``'s ``reply_description``) in ``params`` so a consumer sees WHICH option
    was tapped, not just its label — never a 5xx that would have Meta redeliver the poison
    tap forever.

    The pending question is read NON-destructively first (``peek_pending``) and only
    popped once the tap is confirmed a real answer, so a stale/malformed tap never
    claims a live ask that a concurrent genuine reply from the same pair could still
    answer. On the answer path the tap's id is consumed to select the option and the
    correlated-answer seam carries no params, so nothing is forwarded there.
    """
    if await already_seen(wamid):
        return
    interactive = message.get("interactive")
    if isinstance(interactive, dict) and interactive.get("type") == "nfm_reply":
        # A completed WhatsApp Flow form arrives as an nfm_reply, not a button/list tap.
        await _handle_form_reply(interactive, phone_number_id, wa_id, wamid, params)
        return
    reply_id, title, description = _extract_interactive_reply(interactive)
    reply_params: dict[str, str] = {}
    _put_param(reply_params, "reply_id", reply_id)
    _put_param(reply_params, "reply_description", description)

    # Peek the pending ask (non-destructive) and check the tap against it. A non-answer
    # must not touch the pending — bridge the tap's title and leave the ask untouched.
    pending = await peek_pending(phone_number_id, wa_id)
    if pending is None:
        await _bridge_inbound(phone_number_id, wa_id, title, wamid, params=_merged_params(params, reply_params))
        return
    answer = _map_tap_to_answer(reply_id, pending)
    if answer is None:
        # A stale/malformed/out-of-range tap, or a text ask with no options — not an
        # answer to this pending ask; bridge the tap's title and leave the ask intact.
        await _bridge_inbound(phone_number_id, wa_id, title, wamid, params=_merged_params(params, reply_params))
        return
    # A real answer — resolve it via the shared ladder (which peeks + forwards + keeps
    # or releases). One-pending is enforced on reserve, and the wamid dedupe above
    # guards a redelivery, so the peek-then-resolve needs no destructive claim.
    await _resolve_answer(phone_number_id, wa_id, wamid, answer, pending, params=_merged_params(params, reply_params))


async def _handle_button(
    message: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str, params: dict[str, str]
) -> None:
    """A template quick-reply tap (a ``button`` message): resolve its visible text, else route to the bridge.

    Resolves ``button.text`` against a pending question via the shared ladder, with the same
    routing and known-contact semantics a text message takes.

    ``button.text`` is the human-visible label (the turn text every consumer sees);
    ``button.payload`` is the developer-defined payload behind it, carried in ``params`` as
    ``button_payload`` on the bridged turn. On the correlated-answer path the visible text
    answers the ask (as a typed reply would) and the params seam carries nothing.
    """
    if await already_seen(wamid):
        return
    button = message.get("button")
    text_value = button.get("text") if isinstance(button, dict) else None
    text = text_value if isinstance(text_value, str) else ""
    payload = button.get("payload") if isinstance(button, dict) else None
    button_params: dict[str, str] = {}
    _put_param(button_params, "button_payload", payload)

    pending = await peek_pending(phone_number_id, wa_id)
    if pending is None:
        await _bridge_inbound(phone_number_id, wa_id, text, wamid, params=_merged_params(params, button_params))
        return
    # A quick-reply while a question is pending answers with its visible text minus outer
    # whitespace, mirroring a typed reply.
    await _resolve_answer(
        phone_number_id, wa_id, wamid, text.strip(), pending, params=_merged_params(params, button_params)
    )
