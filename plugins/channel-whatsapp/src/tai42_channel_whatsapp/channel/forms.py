"""WhatsApp-Flow form send: the form ask (ask delivery) and the ask-less notify.

Both build and validate the per-send Flow and its data before any network work,
resolve one published Flow per Flow identity (the schema hash and the data-endpoint
URI; found on the WABA by name, else created, and cached under the WABA id), and send
the Flow message. The ask reserves a correlation; the ask-less notify rides a
``tai42-nf:`` token instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import uuid4

from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelInputError, ChannelNotification
from tai42_contract.interactions.models import FormReactions, FormWording

from tai42_channel_whatsapp.channel.media import _send_media_prelude
from tai42_channel_whatsapp.client import (
    FlowSummary,
    create_flow,
    delete_flow,
    list_flows,
    publish_flow,
    send_flow,
)
from tai42_channel_whatsapp.correlation import (
    cache_flow_form,
    cache_flow_id,
    cache_reaction_form,
    flow_resolve_lock,
    get_cached_flow_id,
    release_pending,
    reserve_pending,
)
from tai42_channel_whatsapp.flows import FORM_ENTRY_SCREEN, build_flow_data, build_form_flow, payload_labels
from tai42_channel_whatsapp.settings import (
    WhatsAppSettings,
    require_delivery_secret,
    require_delivery_setting,
    whatsapp_settings,
)

logger = logging.getLogger(__name__)

# The flow-token namespace for an ASK-LESS form notification: prefix + schema hash
# + a random suffix. Inbound routing branches on this prefix BEFORE any pending-ask
# lookup, so a notify-form submission can never answer (or disturb) a question
# pending on the same pair — while an ask's flow token stays its interaction id
# verbatim and never enters this namespace. The prefix is wire-visible in every
# delivered form's token; changing it orphans the forms already sitting in chats.
_NOTIFY_FORM_TOKEN_PREFIX = "tai42-nf:"  # noqa: S105 constant identifier, not a secret value

# A Flow's name on Meta is this prefix + the Flow identity (:func:`_flow_identity`).
# Meta enforces name uniqueness per WABA, and the name is the lookup key on the WABA
# when the flow-id cache has no entry. The identity covers the schema hash and the
# endpoint URI — everything the create sends that shapes the Flow — so a published
# (immutable) Flow's name vouches for its content.
_FLOW_NAME_PREFIX = "tai42-form-"

# Meta's Flow statuses, as Meta spells them. A live Flow is the plugin's published Flow
# (THROTTLED / BLOCKED only flag an unhealthy endpoint, which Meta's monitoring lifts);
# a DRAFT is editable, so its name does not vouch for its content; a DEPRECATED Flow can
# no longer be sent, deleted or restored.
_LIVE_FLOW_STATUSES = frozenset({"PUBLISHED", "THROTTLED", "BLOCKED"})
_DRAFT_FLOW_STATUS = "DRAFT"
_DEPRECATED_FLOW_STATUS = "DEPRECATED"

# The resolve lock's lifetime and a waiter's longest wait, as a multiple of the HTTP
# timeout setting. Nothing bounds the holder's own work to it: when the lock expires
# while the holder still works, a waiter may resolve too, and its create under the same
# name is refused by Meta as not unique and raises.
_RESOLVE_LOCK_HTTP_TIMEOUTS = 6

# Meta's cap on the Flow message's ``flow_cta`` (the chat button that opens the Flow): up to 20
# characters, no emoji. LIVE-CITE(flow_cta cap): the documented numbers disagree (pywa mirrors 20,
# Meta's "Sending a Flow" guide advises 30), so the stricter 20 is enforced here and the live API's
# actual refusal boundary is proved on the live leg before this constant is loosened.
_OPEN_LABEL_MAX_CHARS = 20

# Meta caps the Flow message's ``interactive.body.text`` at this many characters. A door-rejection
# re-send carries the caller's refusal sentence (``wording.answer.rejected``) with the door's own
# reason substituted into every ``{reason}`` token, so the caller's template must leave room for the
# reason: the door line is bounded at ``_DOOR_REJECTION_MAX_CHARS`` before it rides the body, and the
# template's worst-case expansion (its fixed text plus every token filled) is capped at the body size.
_FLOW_BODY_MAX_CHARS = 1024
_DOOR_REJECTION_MAX_CHARS = 500


def _form_wording(send: ChannelDelivery | ChannelNotification) -> FormWording | None:
    """The send's ``wording.form`` chapter, or ``None`` when the send carried no form wording."""
    return send.wording.form if send.wording is not None else None


def _guard_open_label(label: str | None) -> None:
    """Refuse a caller ``wording.form.open`` over Meta's ``flow_cta`` cap, before any store or send.

    A label the chat button cannot carry is a permanent input refusal (:class:`ChannelInputError`),
    never truncated and never retried.
    """
    if label is not None and len(label) > _OPEN_LABEL_MAX_CHARS:
        raise ChannelInputError(
            f"form open-button label is {len(label)} characters; WhatsApp caps the flow button "
            f"(flow_cta) at {_OPEN_LABEL_MAX_CHARS} characters"
        )


def _guard_rejected_template(template: str | None) -> None:
    """Refuse a caller ``wording.answer.rejected`` too long to ride a door-rejection re-send, up front.

    A refused form answer is re-asked with a fresh Flow whose body is the caller's refusal sentence
    with the door's own reason substituted for EACH ``{reason}`` token. Every substitution is bounded
    at ``_DOOR_REJECTION_MAX_CHARS`` and Meta caps the body at ``_FLOW_BODY_MAX_CHARS``, so the
    template's worst-case expansion — its fixed text plus every ``{reason}`` filled — must fit the
    body; otherwise the re-send would strand the participant with neither the Flow nor a message. A
    template over the cap is a permanent input refusal (:class:`ChannelInputError`), checked at
    deliver — never at inbound time, where a re-send Meta refuses has no recovery.
    """
    if template is None:
        return
    reason_count = template.count("{reason}")
    worst_case = len(template.replace("{reason}", "")) + reason_count * _DOOR_REJECTION_MAX_CHARS
    if worst_case > _FLOW_BODY_MAX_CHARS:
        raise ChannelInputError(
            f"wording.answer.rejected expands to up to {worst_case} characters once each of its "
            f"{reason_count} {{reason}} token(s) is filled (up to {_DOOR_REJECTION_MAX_CHARS} characters each); "
            f"WhatsApp caps the re-sent Flow body at Meta's {_FLOW_BODY_MAX_CHARS}-character limit"
        )


def _reverse_payload_labels(schema: dict[str, Any]) -> dict[str, str]:
    """The completion-payload-label → schema-key reverse map for a form schema.

    Inverts :func:`payload_labels` (schema key → human-readable payload label) — the single
    mapping definition both send paths, the re-send and the inbound decode share. The label
    map is injective, so the inversion is lossless. Stored on the pending record and the
    notify sidecar so the reply decode reads the map, never re-derives it.
    """
    return {label: key for key, label in payload_labels(schema["properties"]).items()}


def _flow_identity(schema_hash: str, endpoint_uri: str | None) -> str:
    """sha256 hex over the schema hash and the endpoint URI: the published Flow's identity."""
    canonical = json.dumps(
        {"schema_hash": schema_hash, "endpoint_uri": endpoint_uri}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _find_named_flow(waba_id: str, name: str) -> str | None:
    """The live Flow already named ``name`` on the WABA, or ``None`` once the name is clear to create.

    A live match is returned for adoption. A DRAFT match is deleted (a failed delete
    raises: a create beside it would clash on the name). A DEPRECATED match is logged
    and left alone; the create's own answer then says whether Meta still reserves its
    name. Any other status, or more than one live match, raises.
    """
    matches = [flow for flow in await list_flows(waba_id) if flow.name == name]
    known = _LIVE_FLOW_STATUSES | {_DRAFT_FLOW_STATUS, _DEPRECATED_FLOW_STATUS}
    unknown = [flow for flow in matches if flow.status not in known]
    if unknown:
        raise ChannelDeliveryError(
            "WhatsApp lists "
            + ", ".join(f"flow {flow.flow_id} named {name} in unrecognised status {flow.status}" for flow in unknown)
        )
    live = [flow for flow in matches if flow.status in _LIVE_FLOW_STATUSES]
    if len(live) > 1:
        raise ChannelDeliveryError(
            f"WhatsApp lists {len(live)} live flows named {name}: {_flow_ids(live)}; a flow name is unique per WABA"
        )
    if live:
        return live[0].flow_id
    for draft in (flow for flow in matches if flow.status == _DRAFT_FLOW_STATUS):
        await delete_flow(draft.flow_id)
    deprecated = [flow for flow in matches if flow.status == _DEPRECATED_FLOW_STATUS]
    if deprecated:
        logger.warning(
            "deprecated flow(s) %s already carry name %s on WABA %s; creating a new flow",
            _flow_ids(deprecated),
            name,
            waba_id,
        )
    return None


def _flow_ids(flows: list[FlowSummary]) -> str:
    return ", ".join(flow.flow_id for flow in flows)


async def _resolve_flow_id(
    waba_id: str, schema_hash: str, flow_json: dict[str, Any], endpoint_uri: str | None = None
) -> str:
    """The published flow id for this Flow under ``waba_id``.

    The Flow's identity is :func:`_flow_identity` of ``schema_hash`` and ``endpoint_uri``;
    it names the Flow on Meta and keys the flow-id cache. The cached id, else — under the
    resolve lock, re-reading the cache once held — the live Flow found on the WABA by name
    (see :func:`_find_named_flow`), else create + publish + store a new Flow.
    ``endpoint_uri`` links an endpoint-driven (reacting) Flow to the plugin's data
    endpoint at create; a static Flow passes none. Every step is loud — a list, delete,
    create, publish, or store failure raises and never falls back to another answer format.

    Meta returns HTTP 200 for a create even when the Flow JSON is invalid: the draft
    IS created but carries ``validation_errors`` and can never publish. A non-empty
    list is refused loudly with the full list rendered, BEFORE ``publish_flow``.

    That refusal — and a publish or cache failure AFTER a successful create — strands
    the draft on Meta, so the draft is best-effort deleted before re-raising the
    original error; a delete that itself fails is logged without masking it, and the
    next resolve finds that draft by name and deletes it. This is the ONE writer of a
    Flow's lifecycle.
    """
    identity = _flow_identity(schema_hash, endpoint_uri)
    cached = await get_cached_flow_id(waba_id, identity)
    if cached is not None:
        return cached
    ttl_seconds = _RESOLVE_LOCK_HTTP_TIMEOUTS * whatsapp_settings().http_timeout_seconds
    async with flow_resolve_lock(waba_id, identity, ttl_seconds=ttl_seconds):
        cached = await get_cached_flow_id(waba_id, identity)
        if cached is not None:
            return cached
        name = f"{_FLOW_NAME_PREFIX}{identity}"
        found = await _find_named_flow(waba_id, name)
        if found is not None:
            await cache_flow_id(waba_id, identity, found)
            return found
        result = await create_flow(waba_id=waba_id, name=name, flow_json=flow_json, endpoint_uri=endpoint_uri)
        try:
            if result.validation_errors:
                # The refusal rides the SAME except that deletes an orphaned draft, so the
                # invalid draft is cleaned up by the one lifecycle writer — hence raising here.
                raise ChannelDeliveryError(  # noqa: TRY301
                    "WhatsApp created the flow draft with validation errors and it can never publish: "
                    + json.dumps(result.validation_errors, sort_keys=True, separators=(",", ":"))[:1000]
                )
            await publish_flow(result.flow_id)
            await cache_flow_id(waba_id, identity, result.flow_id)
        except Exception:
            try:
                await delete_flow(result.flow_id)
            except Exception:
                logger.exception("failed to delete orphaned draft flow %s after resolve failure", result.flow_id)
            raise
        return result.flow_id


def _form_pages_list(send: ChannelDelivery | ChannelNotification) -> list[dict[str, Any]] | None:
    """The form's step layout as plain JSON, or ``None`` when the send carried one page.

    Each page is ``{"title", "fields", "display", "kind"}`` — the display blocks (heading/body/
    image, each static or slotted) and the input/review kind ride alongside the field list, so
    the renderer draws display elements and a review screen. Shared by the form
    ask and the ask-less form notification, whose ``pages`` field is the identical shape.
    """
    if send.pages is None:
        return None
    return [
        {
            "title": page.title,
            "fields": list(page.fields),
            "kind": page.kind,
            "display": [block.model_dump(exclude_none=True) for block in page.display],
            **({"button_label": page.button_label} if page.button_label is not None else {}),
        }
        for page in send.pages
    ]


def _reactions_dict(reactions: FormReactions | None) -> dict[str, Any] | None:
    """The form's reaction triggers as plain JSON, or ``None`` when the form does not react.

    A reactions block with no trigger (no field change, page advance, or submit) is a static
    form, so it yields ``None`` — the Flow stays the navigate (endpoint-less) Flow.
    """
    if reactions is None or not reactions.has_trigger():
        return None
    return {
        "field_changed": list(reactions.field_changed),
        "page_advanced": list(reactions.page_advanced),
        "submitted": reactions.submitted,
        "choices": list(reactions.choices),
    }


def _require_reaction_settings(settings: WhatsAppSettings) -> str:
    """The data-endpoint URI a reacting form publishes against; raise loudly when key material is missing.

    A reacting form CANNOT publish or be served without the business private key (to decrypt the
    endpoint's requests) and the public endpoint URI (configured on the Flow so Meta routes
    data-exchange back here). Either unset is refused loudly — never a silent fall back to a
    non-reacting flow. The public key must also be registered for the sending number (operator
    step; see :func:`tai42_channel_whatsapp.provisioning.provision_flow_encryption`).
    """
    require_delivery_secret(settings.flow_private_key, "CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY")
    return require_delivery_setting(settings.flow_endpoint_uri, "CHANNEL_WHATSAPP_FLOW_ENDPOINT_URI")


def _form_values_and_options(
    send: ChannelDelivery | ChannelNotification,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """The form's per-send ``values`` and ``options`` as plain JSON.

    Each option is ``{"value", "label"?, "description"?}`` (``label`` and the second-line
    ``description`` each omitted when absent). Empty when the send carried no data.
    Shared by the form ask and the ask-less form notification, whose ``data`` field is
    the identical shape.
    """
    if send.data is None:
        return {}, {}
    options: dict[str, list[dict[str, Any]]] = {}
    for name, choices in send.data.options.items():
        options[name] = [
            {
                "value": choice.value,
                **({"label": choice.label} if choice.label is not None else {}),
                **({"description": choice.description} if choice.description is not None else {}),
            }
            for choice in choices
        ]
    return dict(send.data.values), options


async def send_form_ask_flow(
    phone_number_id: str,
    to: str,
    body_text: str,
    flow_token: str,
    schema: dict[str, Any],
    pages: list[dict[str, Any]] | None,
    values: dict[str, Any],
    options: dict[str, list[dict[str, Any]]],
    reactions: dict[str, Any] | None = None,
    open_label: str | None = None,
    form_title: str | None = None,
    form_submit: str | None = None,
) -> str:
    """Build, resolve and send a form ask's Flow; return its ``wamid``.

    The ONE sender for a form ask's Flow: the first send and every door-rejection
    re-send call it with the same inputs the pending record holds, so the re-send
    resolves the SAME published Flow through the same Flow identity — found on the WABA
    by name, else re-created, if the cache was lost, never raising a stale-lookup miss —
    and navigates to the entry screen with the same prefill and option lists the first
    send carried. ``reactions`` (present for a reacting form) publishes the Flow as
    ENDPOINT-DRIVEN against the configured data endpoint; the reacting flow and the
    endpoint URI fold into the Flow identity, so its published Flow is distinct from the
    static one and from one bound to another endpoint.

    ``open_label`` is the caller's text for the chat button that opens the Flow, drawn as the
    message ``flow_cta`` (not Flow JSON, so it never re-keys the Flow); ``form_title`` is the
    caller's screen title and ``form_submit`` the caller's terminal-button label for a page-less
    form (both Flow JSON, so they DO re-key). The re-send passes the stored values of all three, so
    the correction message draws the same button, title and submit label.
    """
    flow_json, schema_hash = build_form_flow(schema, pages, set(options), reactions, form_title, form_submit)
    flow_data = build_flow_data(schema, values, options, pages)
    settings = whatsapp_settings()
    waba_id = require_delivery_setting(settings.waba_id, "CHANNEL_WHATSAPP_WABA_ID")
    endpoint_uri = _require_reaction_settings(settings) if reactions is not None else None
    flow_id = await _resolve_flow_id(waba_id, schema_hash, flow_json, endpoint_uri)
    return await send_flow(
        phone_number_id=phone_number_id,
        to=to,
        body_text=body_text,
        flow_id=flow_id,
        flow_token=flow_token,
        screen=FORM_ENTRY_SCREEN,
        data=flow_data,
        open_label=open_label,
    )


async def _deliver_form(
    settings: WhatsAppSettings, phone_number_id: str, target: str, delivery: ChannelDelivery
) -> None:
    """Deliver a form ask as a WhatsApp Flow (Tier-2: reserve BEFORE send).

    The Flow (one screen per page) and its per-send data are built and validated
    BEFORE any network work — an unsupported schema, an unmappable per-send option, or
    an unknown page field raises here, before the ``CHANNEL_WHATSAPP_WABA_ID`` gate, the
    reservation, or a send. The reservation carries the answer schema (so an inbound
    Flow response is coerced to its types), the question text (so a door-rejected answer
    is re-asked), the per-send pages/values/options (so a re-send reproduces the same
    Flow) and the payload-label reverse map (so the reply's label-keyed answers map
    back to the schema keys), and uses the ``interaction_id`` as the ``flow_token`` correlating the
    completed form. The published Flow is keyed by the ``(schema, pages, option_fields)``
    triple, the emitted shape (the option-bearing fields decide which string
    properties render as dropdowns) and the data-endpoint URI, and REUSED across sends
    (found on the WABA by name, else created, and cached under the WABA id); the prefilled values and
    per-send option lists ride the send's ``flow_action_payload.data`` (a dynamic
    data-source), never a new Flow. A REACTING form (one carrying ``reactions`` triggers)
    publishes the Flow endpoint-driven and refuses loudly, before any reservation, when the
    data-endpoint key material is missing (:func:`_require_reaction_settings`); its schema +
    per-send inputs are cached under the ``interaction_id`` so the data endpoint can serve its
    reactions. The send goes through the one sender :func:`send_form_ask_flow`; a failure
    resolving the Flow or sending releases the reservation and raises — never a fallback format.
    """
    if delivery.schema is None:
        raise ChannelDeliveryError(f"form delivery {delivery.interaction_id} is missing its schema")
    pages = _form_pages_list(delivery)
    values, options = _form_values_and_options(delivery)
    reactions = _reactions_dict(delivery.reactions)
    form = _form_wording(delivery)
    open_label = form.open if form is not None else None
    form_title = form.title if form is not None else None
    form_submit = form.submit if form is not None else None
    answer = delivery.wording.answer if delivery.wording is not None else None
    rejected_template = answer.rejected if answer is not None else None
    # Build + validate before any reservation (raises on an unsupported schema, an
    # unmappable per-send option, an unknown page field, an unrenderable reaction trigger, an
    # over-cap open-button label / footer label, or an over-cap rejection template), so an
    # unrenderable send reserves nothing.
    _guard_open_label(open_label)
    _guard_rejected_template(rejected_template)
    build_form_flow(delivery.schema, pages, set(options), reactions, form_title, form_submit)
    build_flow_data(delivery.schema, values, options, pages)
    require_delivery_setting(settings.waba_id, "CHANNEL_WHATSAPP_WABA_ID")
    # A reacting form cannot publish or be served without its data-endpoint key material —
    # refuse loudly up front (never a silent fall back to a non-reacting flow).
    if reactions is not None:
        _require_reaction_settings(settings)

    await reserve_pending(
        phone_number_id=phone_number_id,
        wa_id=target,
        callback_url=delivery.callback_url,
        timeout_at=delivery.timeout_at,
        interaction_id=delivery.interaction_id,
        schema=delivery.schema,
        question=delivery.question,
        form_pages=pages,
        form_values=values,
        form_options=options,
        form_names=_reverse_payload_labels(delivery.schema),
        form_reactions=reactions,
        on_mismatch=delivery.on_mismatch,
        wording=delivery.wording,
    )
    # A reacting form's data endpoint recovers its schema + inputs from this sidecar by the
    # flow token (the interaction id); it expires with the ask's own deadline.
    if reactions is not None:
        await cache_reaction_form(delivery.interaction_id, delivery.schema, pages, values, options, delivery.timeout_at)
    try:
        await send_form_ask_flow(
            phone_number_id=phone_number_id,
            to=target,
            body_text=delivery.question,
            flow_token=delivery.interaction_id,
            schema=delivery.schema,
            pages=pages,
            values=values,
            options=options,
            reactions=reactions,
            open_label=open_label,
            form_title=form_title,
            form_submit=form_submit,
        )
    except Exception:
        # Any create/publish/store/send failure frees the pair instead of holding
        # it until its TTL.
        await release_pending(phone_number_id=phone_number_id, wa_id=target)
        raise


async def _send_form_notification(
    settings: WhatsAppSettings, phone_number_id: str, target: str, notification: ChannelNotification
) -> list[str]:
    """Send an ask-less form notification, returning every ``wamid`` in send order.

    Any display media rides as the standard prelude (the ``link`` items as one
    text line-block, each ``image`` as its own message), then the Flow message
    LAST — the actionable prompt stays at the foot of the chat. The Flow is
    resolved exactly like a form ask's (one published Flow per answer
    schema, found on the WABA by name, else created, and cached under the WABA id),
    and the answer schema itself (with its
    payload-label reverse map) is cached beside
    the flow id — the submission's reply carries only the schema hash inside its
    flow token, so that sidecar is the ONLY place the inbound side can recover the
    schema and map to decode and coerce the values (see :func:`cache_flow_form`). NO correlation is
    reserved: the token — minted in the ``tai42-nf:`` namespace as prefix + schema
    hash + a random suffix — routes the reply, not the pair, so any number of forms
    may be outstanding and a pending ask on the same pair is never touched.

    A caller-set ``form_tag`` rides the token as a 4th ``:``-delimited segment
    (``tai42-nf:{hash}:{nonce}:{form_tag}``) when set — its charset excludes ``:`` so it
    stays one segment. It NEVER enters the published Flow artefact, the schema hash, the
    cached flow id or the send's data, so it triggers no republish and leaves the artefact
    recipient-data-free; the reply side reads it straight back off the returned token.
    """
    if notification.schema is None:  # dispatch guard; notify() branches on the field
        raise ChannelDeliveryError("form notification is missing its schema")
    # Mirror the form ask: the per-send Flow (one screen per page) and its prefill/option
    # data are built and validated BEFORE any network work, and the send navigates to the
    # entry screen injecting that data — so an ask-less form opens already filled in. The
    # published Flow is keyed by the ``(schema, pages, option_fields)`` triple and reused
    # across sends; the prefilled values and per-send option lists ride the send's data.
    pages = _form_pages_list(notification)
    values, options = _form_values_and_options(notification)
    form = _form_wording(notification)
    open_label = form.open if form is not None else None
    form_title = form.title if form is not None else None
    form_submit = form.submit if form is not None else None
    _guard_open_label(open_label)
    flow_json, schema_hash = build_form_flow(
        notification.schema, pages, set(options), form_title=form_title, form_submit=form_submit
    )
    flow_data = build_flow_data(notification.schema, values, options, pages)
    waba_id = require_delivery_setting(settings.waba_id, "CHANNEL_WHATSAPP_WABA_ID")

    sent = await _send_media_prelude(phone_number_id, target, list(notification.media or []))
    flow_id = await _resolve_flow_id(waba_id, schema_hash, flow_json)
    # Written beside every flow-id use — the reply side cannot repopulate it. The
    # payload-label reverse map rides with the schema so the reply decodes to schema keys.
    await cache_flow_form(waba_id, schema_hash, notification.schema, _reverse_payload_labels(notification.schema))
    flow_token = f"{_NOTIFY_FORM_TOKEN_PREFIX}{schema_hash}:{uuid4().hex}"
    if notification.form_tag is not None:
        # The caller-set tag rides as the 4th token segment; it never touches the Flow
        # artefact, the hash or the send data, so no republish and no recipient data in the artefact.
        flow_token = f"{flow_token}:{notification.form_tag}"
    sent.append(
        await send_flow(
            phone_number_id=phone_number_id,
            to=target,
            body_text=notification.message,
            flow_id=flow_id,
            flow_token=flow_token,
            screen=FORM_ENTRY_SCREEN,
            data=flow_data,
            open_label=open_label,
        )
    )
    return sent
