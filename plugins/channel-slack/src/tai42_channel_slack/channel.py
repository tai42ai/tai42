"""The Slack ``Channel`` — outbound sends via ``chat.postMessage``: ``deliver``
(questions) and ``notify`` (fire-and-forget).

ANY failure raises :class:`~tai42_contract.channels.ChannelDeliveryError`, never
a silent drop. Slack reports most send failures as HTTP 200 with
``{"ok": false, "error": …}``, so success is decided by the JSON ``ok`` field,
not the HTTP status. ``notify`` shares recipient resolution and send validation
but posts a plain message (no correlation, no deadline, no reply) and returns the
posted ``ts``. A ``notify`` carrying ``sender_identity`` must name this
deployment's single bot identity (``CHANNEL_SLACK_BOT_USER_ID``) or it is refused.

The recipient allowlist governs ask_user deliveries; a bridge reply goes solely to
the conversation that initiated it, resolved by the bridge, not the allowlist.

Tier-1 (``confirm``/``external``) skip correlation: their answers travel through
the callback door directly (delivered with the callback URL as a plain link),
never a typed chat reply. Only ``text`` and ``select`` take the Tier-2
typed-reply path.

``form`` questions (``supports_form_delivery``) take a third path: the message
carries a button that opens a Block Kit modal, and the answer arrives as a
``view_submission`` on the interactivity door — the form record reserved here (in
Redis, before the send) holds the schema and callback URL that door needs.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
from pydantic import SecretStr
from tai42_contract.channels import (
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelInputError,
    ChannelNotification,
)
from tai42_kit.settings import require, require_secret

from tai42_channel_slack.blocks import (
    build_flat_option_blocks,
    build_footer_block,
    build_header_blocks,
    build_location_block,
    build_media_blocks,
    build_option_blocks,
    build_section_blocks,
    flat_options_text_lines,
    location_text_line,
    options_text_lines,
    sections_text_lines,
    text_section,
)
from tai42_channel_slack.client import slack_http
from tai42_channel_slack.correlation import (
    delete_form_record,
    remaining_seconds,
    store_correlation,
    store_form_record,
)
from tai42_channel_slack.forms import (
    FormSchemaError,
    build_message_blocks,
    build_modal_view,
)
from tai42_channel_slack.forms import validate_form_schema as _validate_form_schema
from tai42_channel_slack.settings import SlackSettings, slack_settings

# Answered through the callback door directly (tappable plain link), never a typed
# thread reply: confirm (door records a bool from the tap) and external (structured POST).
_TIER1_FORMATS = frozenset({"confirm", "external"})


def _require_for_delivery[T](value: T | None, env_name: str) -> T:
    """The configured value, or :class:`ChannelDeliveryError` naming the env var
    (a config gap on the deliver/notify path is a delivery failure; wraps
    ``require``, raised before any network call)."""
    try:
        return require(value, "the slack channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _require_secret_for_delivery(value: SecretStr | None, env_name: str) -> str:
    """The secret's plaintext, or :class:`ChannelDeliveryError` on unset/EMPTY
    (fail closed; wraps ``require_secret``, message names only the env var)."""
    try:
        return require_secret(value, "the slack channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _render_text(delivery: ChannelDelivery) -> str:
    """The question as Slack message text. Tier-2 (``text``/``select``) carries the
    reply-in-thread instruction (the thread is the correlation key); Tier-1
    (``confirm``/``external``) carries the callback URL as a plain link. ``select``
    enumerates its options inline; the answer is validated at the callback door."""
    lines = [delivery.question]
    if delivery.answer_format in _TIER1_FORMATS:
        lines.append(f"Answer here: {delivery.callback_url}")
        lines.append(f"Deadline: {delivery.timeout_at.isoformat()}")
        return "\n".join(lines)
    if delivery.options:
        lines.append("Options: " + ", ".join(delivery.options))
    lines.append(f"Reply in this thread. Deadline: {delivery.timeout_at.isoformat()}")
    return "\n".join(lines)


def _display_blocks(body_text: str, media: list[Any] | None, options: list[str] | None) -> list[dict[str, Any]] | None:
    """The Block Kit blocks for a richer (non-form) send, or ``None`` for a plain send.

    A body section carries the question/message, then any display media (image / link
    blocks), then the options as an actions block of buttons when they fit Slack's caps
    — else a suggestion section so they stay visible (a tapped button submits its text;
    an over-cap select still answers by a thread reply, a notify option by a typed
    reply). ``None`` when there is neither media nor options, so a plain ask/notify
    posts text only (unchanged). A blank ``body_text`` (a MEDIA-ONLY notify) OMITS the body
    section entirely — Slack rejects an empty ``plain_text`` — so the blocks are the image
    block(s) alone. A ``data:`` image raises
    :class:`~tai42_contract.channels.ChannelInputError` here, before any send.
    """
    media_blocks = build_media_blocks(media)
    option_blocks = build_option_blocks(options)
    if options and not option_blocks:
        # Past Slack's button caps — keep the options visible as a suggestion list
        # rather than dropping them or truncating a label.
        option_blocks = [text_section(options_text_lines(options))]
    if not media_blocks and not option_blocks:
        return None
    body_section = [text_section(body_text)] if body_text.strip() else []
    return [*body_section, *media_blocks, *option_blocks]


def _notification_blocks(notification: ChannelNotification) -> list[dict[str, Any]] | None:
    """The Block Kit blocks for a ``notify`` send, or ``None`` for a plain text-only send.

    Order: an optional header display item, then the message body section (omitted when the
    message is blank — a content-only send), then display media, a shared location, the
    interactive choice surface (flat options OR a sectioned list), and finally the footer.
    A ``data:`` image (header or media) raises :class:`ChannelInputError` here, before any
    send. A :class:`ChannelTemplate` and a form ``schema`` are not renderable on this channel
    (see the capability notes on :class:`SlackChannel`) and are refused loudly rather than
    silently dropped, defending the channel even if a caller reaches it past the guards.
    """
    if notification.template is not None:
        raise ChannelInputError(
            "slack cannot render a vendor template: it has no approved-template registry, so the "
            "template's substitution parameters have no message skeleton to render into"
        )
    if notification.schema is not None:
        raise ChannelInputError(
            "slack does not render ask-less form notifications; it has no notify-form submission surface"
        )
    # The rich content that WARRANTS Block Kit (header/media/location/options/sections/
    # footer). With none of it a plain message posts text only (no blocks), unchanged.
    header_blocks = build_header_blocks(notification.header)
    media_blocks = build_media_blocks(notification.media)
    location_blocks = [build_location_block(notification.location)] if notification.location is not None else []
    option_blocks = build_flat_option_blocks(notification.options) if notification.options else []
    section_blocks = build_section_blocks(notification.sections) if notification.sections else []
    footer_blocks = [build_footer_block(notification.footer)] if notification.footer is not None else []
    rich = [*header_blocks, *media_blocks, *location_blocks, *option_blocks, *section_blocks, *footer_blocks]
    if not rich:
        return None
    # A blank message (a content-only send) OMITS the body section — Slack rejects an empty
    # plain_text — so the rich blocks carry it alone. The header stays ABOVE the body.
    body_section = [text_section(notification.message)] if notification.message.strip() else []
    return [
        *header_blocks,
        *body_section,
        *media_blocks,
        *location_blocks,
        *option_blocks,
        *section_blocks,
        *footer_blocks,
    ]


def _notification_text_fallback(notification: ChannelNotification) -> str:
    """The ``text`` field Slack requires alongside blocks. It carries the message plus the
    interactive/location content as suggestion lines so the notification PREVIEW (the push
    Slack shows before blocks render) still surfaces them; visual-only content (media,
    header, footer) rides the blocks alone. Blank for a content-only send."""
    parts = [notification.message] if notification.message.strip() else []
    if notification.options:
        parts.append(flat_options_text_lines(notification.options))
    if notification.sections:
        parts.append(sections_text_lines(notification.sections))
    if notification.location is not None:
        parts.append(location_text_line(notification.location))
    return "\n".join(parts)


def _resolve_recipient(settings: SlackSettings, requested: str | None) -> str:
    """The conversation id to send to.

    A caller-requested recipient must be on the operator allowlist or is refused
    loudly (fail CLOSED; empty allowlist rejects every request); no requested
    recipient uses the operator-set default (no allowlist check). Missing config
    raises :class:`ChannelDeliveryError` naming the env var.
    """
    if requested is None:
        return _require_for_delivery(settings.default_recipient, "CHANNEL_SLACK_DEFAULT_RECIPIENT")
    if requested not in set(settings.allowed_recipients):
        raise ChannelDeliveryError(
            f"recipient {requested!r} is not on CHANNEL_SLACK_ALLOWED_RECIPIENTS; refusing to send"
        )
    return requested


async def _post_message(
    token: str, target: str, text: str, blocks: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """POST one ``chat.postMessage`` and return its validated JSON body.

    ``blocks`` (Block Kit) rides alongside ``text``; ``text`` is kept as the
    notification fallback Slack requires even when blocks carry the content. Any
    failure — transport error, non-200 status, non-JSON body, or ``ok`` not true —
    raises :class:`ChannelDeliveryError`. Slack answers HTTP 200 even for errors,
    so the JSON ``ok`` field is the ONLY success signal.
    """
    payload: dict[str, Any] = {"channel": target, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    try:
        async with slack_http() as client:
            response = await client.post(
                f"{slack_settings().api_base_url}/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"chat.postMessage transport failure: {exc}") from exc
    if response.status_code != 200:
        raise ChannelDeliveryError(f"chat.postMessage returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError("chat.postMessage returned a non-JSON body") from exc
    if body.get("ok") is not True:
        raise ChannelDeliveryError(f"chat.postMessage failed: {body.get('error', 'unknown error')}")
    return body


async def open_modal_view(trigger_id: str, view: dict[str, Any]) -> None:
    """POST one ``views.open`` for a form question's modal, or raise.

    Called inline from the interactivity door on a ``tai42_form_open`` click, with
    the ``block_actions`` ``trigger_id`` (valid ~3 s). Any failure — transport
    error, non-200 status, non-JSON body, or ``ok`` not true — raises
    :class:`ChannelDeliveryError`; the door lets it surface as a loud 500 so Slack
    reports the failure to open. Settings (bot token, api base) read fresh.
    """
    settings = slack_settings()
    token = _require_secret_for_delivery(settings.bot_token, "CHANNEL_SLACK_BOT_TOKEN")
    try:
        async with slack_http() as client:
            response = await client.post(
                f"{settings.api_base_url}/views.open",
                headers={"Authorization": f"Bearer {token}"},
                json={"trigger_id": trigger_id, "view": view},
            )
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"views.open transport failure: {exc}") from exc
    if response.status_code != 200:
        raise ChannelDeliveryError(f"views.open returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError("views.open returned a non-JSON body") from exc
    if body.get("ok") is not True:
        raise ChannelDeliveryError(f"views.open failed: {body.get('error', 'unknown error')}")


def _form_data_dict(delivery: ChannelDelivery) -> dict[str, Any] | None:
    """The form's per-send ``{values, options}`` as plain JSON for the record and the
    modal builder — each option ``{"value", "label"?}`` (label omitted when absent).
    ``None`` when the ask carried no data."""
    if delivery.data is None:
        return None
    options: dict[str, list[dict[str, Any]]] = {}
    for name, choices in delivery.data.options.items():
        options[name] = [
            {"value": choice.value, **({"label": choice.label} if choice.label is not None else {})}
            for choice in choices
        ]
    return {"values": delivery.data.values, "options": options}


def _form_pages_list(delivery: ChannelDelivery) -> list[dict[str, Any]] | None:
    """The form's step layout as plain JSON — each page ``{"title", "fields"}`` — or
    ``None`` when the ask carried one page."""
    if delivery.pages is None:
        return None
    return [{"title": page.title, "fields": list(page.fields)} for page in delivery.pages]


async def _deliver_form(token: str, target: str, delivery: ChannelDelivery) -> None:
    """Deliver a ``form`` question: post a section + a button whose click opens the
    Block Kit modal, after reserving the form's state.

    The full modal view the click will build is composed here (and discarded)
    BEFORE any Redis or network work — so an unmappable schema OR a modal past a
    Slack cap (including the 100-block cap the click enforces) is a
    :class:`ChannelDeliveryError` naming the fault, and nothing is stored or sent;
    an uncompletable form is never delivered only to 500 at the click. The form
    record is written (reserve-before-send) and released if the send fails, so a
    failed post never leaves a live record with no message behind it.
    """
    schema = delivery.schema
    if not schema:
        # The contract guarantees a non-empty schema for a form; a gap here is a
        # delivery failure, not a silent plain-text send.
        raise ChannelDeliveryError("form answer_format requires a non-empty schema")
    # The per-send prefill/choices and the step layout are what the modal (built AT
    # CLICK TIME) renders, so they ride the record and the compose-and-discard below.
    form_data = _form_data_dict(delivery)
    pages = _form_pages_list(delivery)
    values = form_data.get("values") if form_data is not None else None
    options = form_data.get("options") if form_data is not None else None
    # Compose the exact modal the click will build, discarding it — same single
    # source of truth, so every cap it enforces is enforced here. An unmappable
    # schema, an unmappable per-send extra, or a modal past a Slack cap is a permanent
    # input refusal (:class:`FormSchemaError`, a ``ChannelInputError``), raised before
    # any store or send — never a retryable delivery failure.
    build_modal_view(delivery.interaction_id, delivery.question, schema, values, options, pages)
    # Any display media rides above the form's section + open-modal button (a data:
    # image is refused here, before the reserve or send).
    message_blocks = [
        *build_media_blocks(delivery.media),
        *build_message_blocks(delivery.question, delivery.interaction_id),
    ]
    await store_form_record(
        delivery.interaction_id,
        delivery.callback_url,
        schema,
        delivery.question,
        delivery.timeout_at,
        data=form_data,
        pages=pages,
    )
    try:
        await _post_message(token, target, delivery.question, blocks=message_blocks)
    except ChannelDeliveryError:
        # Reserve-before-send: a failed post releases the reservation so no live
        # form record dangles without a message.
        await delete_form_record(delivery.interaction_id)
        raise


class SlackChannel:
    """Registered under ``"slack"``; satisfies ``tai42_contract.channels.Channel``."""

    # Advertised richer-send capabilities the central notify/answer-delivery guards read
    # before handing a rich notification over:
    #   * media — Block Kit image blocks (image) and labelled link lines (link and, absent
    #     a files.upload seam, document/video/audio);
    #   * interactive — Block Kit option buttons (typed reply/link options) and titled
    #     sectioned option lists;
    #   * location — a section naming the place with an OpenStreetMap link.
    # NOT advertised (honest capability decline, so the platform refuses the send loudly
    # rather than dropping content or sending a meaningless payload):
    #   * template — a :class:`ChannelTemplate` is a pre-approved VENDOR template referenced
    #     by name+language whose body/button text lives in the provider's approved registry;
    #     Slack has no such registry (and no out-of-window restriction), so the model carries
    #     only substitution parameters with no template skeleton to render them into. Slack
    #     cannot reconstruct the message, so it declines the capability;
    #   * form notifications — this channel delivers ask-bound forms (supports_form_delivery)
    #     but has no ask-less notify-form submission surface.
    supports_media_notifications: ClassVar[bool] = True
    supports_interactive_notifications: ClassVar[bool] = True
    supports_location_notifications: ClassVar[bool] = True
    supports_form_delivery: ClassVar[bool] = True

    def validate_form_schema(self, schema: dict[str, Any], question: str) -> None:
        """Enforce this channel's ask-time-knowable Block Kit caps at ask-time,
        before any state is written — the question-text section cap, the supported
        property subset, the label cap, the static-select option-count and
        per-option text caps, and the modal 100-block cap. A violation is refused
        here as a ``ValueError`` so a question or schema the delivery path could
        never render is rejected up front instead of persisting a question that
        only fails at delivery; ``forms`` is the single mapping definition, its
        delivery-time ``FormSchemaError`` becoming the ask-time ``ValueError``."""
        try:
            _validate_form_schema(schema, question)
        except FormSchemaError as exc:
            raise ValueError(str(exc)) from exc

    async def deliver(self, delivery: ChannelDelivery) -> None:
        # Settings read fresh each call so a rotated token or changed recipient
        # policy takes effect on the next question. Missing config is a delivery
        # failure: ChannelDeliveryError naming the env var, before any network call.
        settings = slack_settings()
        token = _require_secret_for_delivery(settings.bot_token, "CHANNEL_SLACK_BOT_TOKEN")
        target = _resolve_recipient(settings, delivery.recipient)
        if remaining_seconds(delivery.timeout_at) <= 0:
            # Never post a question whose budget is already spent.
            raise ChannelDeliveryError(
                f"question budget already expired (timeout_at={delivery.timeout_at.isoformat()})"
            )
        if delivery.answer_format == "form":
            await _deliver_form(token, target, delivery)
            return
        # Display media and tappable options ride as Block Kit blocks alongside the
        # text (the notification fallback Slack requires). A data: image is refused
        # here, before the send. Tier-1 (confirm/external) carries no options — its
        # answer is the plain callback link, so its block body keeps the full rendered
        # text (link included) and only display media rides beneath it.
        is_tier1 = delivery.answer_format in _TIER1_FORMATS
        section_text = _render_text(delivery) if is_tier1 else delivery.question
        block_options = None if is_tier1 else delivery.options
        blocks = _display_blocks(section_text, delivery.media, block_options)
        body = await _post_message(token, target, _render_text(delivery), blocks=blocks)
        if delivery.answer_format in _TIER1_FORMATS:
            # Tier-1 (confirm/external): the plain link IS the answer path — no
            # correlation entry, no threaded reply expected.
            return
        ts = body.get("ts")
        if not isinstance(ts, str) or not ts:
            raise ChannelDeliveryError("chat.postMessage ok response carried no ts")
        await store_correlation(ts, delivery.callback_url, delivery.interaction_id, delivery.timeout_at)

    async def notify(self, notification: ChannelNotification) -> list[str]:
        """Post one plain fire-and-forget message via ``chat.postMessage``, returning
        the posted message ``[ts]``.

        No correlation, no deadline, no reply — the text is sent as-is. Without
        ``sender_identity`` the recipient follows ``deliver``'s allowlist policy;
        with it set (a bridge reply) it must equal this deployment's single bot
        identity — else refused with
        :class:`~tai42_contract.channels.ChannelDeliveryError`, never a send from the
        wrong face — and the recipient is the initiating conversation verbatim,
        allowlist bypassed. A return means Slack ACCEPTED it (not that a human saw
        it); any failure raises ``ChannelDeliveryError``. Settings read fresh.
        """
        settings = slack_settings()
        token = _require_secret_for_delivery(settings.bot_token, "CHANNEL_SLACK_BOT_TOKEN")
        if notification.sender_identity is not None:
            identity = _require_for_delivery(settings.bot_user_id, "CHANNEL_SLACK_BOT_USER_ID")
            if notification.sender_identity != identity:
                raise ChannelDeliveryError(
                    f"sender_identity {notification.sender_identity!r} is not this channel's identity {identity!r}"
                )
            # Bridge reply: send to the initiating conversation verbatim, allowlist bypassed.
            target = (
                notification.recipient
                if notification.recipient is not None
                else _require_for_delivery(settings.default_recipient, "CHANNEL_SLACK_DEFAULT_RECIPIENT")
            )
        else:
            target = _resolve_recipient(settings, notification.recipient)
        # The full richer-send vocabulary rides as Block Kit blocks: header/media/location,
        # typed options (reply buttons submit their text; link buttons open a url) or a
        # sectioned option list, and a footer. A tap on a reply button enters the
        # conversation as a visitor message (the interactivity door bridges it, echoing any
        # author-set option id back as ``params.reply_id``). The text fallback carries the
        # message plus the interactive/location content as suggestion lines so the
        # notification preview still shows them. A MEDIA-ONLY notify (blank message) posts
        # the media block(s) alone with an empty text fallback — Slack accepts that when
        # blocks carry the content. A data: image, a vendor template, or a form schema is
        # refused before the send.
        blocks = _notification_blocks(notification)
        text = _notification_text_fallback(notification)
        body = await _post_message(token, target, text, blocks=blocks)
        ts = body.get("ts")
        if not isinstance(ts, str) or not ts:
            raise ChannelDeliveryError("chat.postMessage ok response carried no ts")
        return [ts]
