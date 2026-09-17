"""Human-messaging facets: webhook verifiers, channels, conversations, and the ask_user facade."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from tai42_contract.channels import Channel, CorrelationStore, InboundAnswerResult, InboundBridge
from tai42_contract.conversations import ConversationTargetKind, DeliveryReceipt, TargetBindValidator
from tai42_contract.interactions.asker import AskUser
from tai42_contract.interactions.models import LocationElement, MediaItem
from tai42_contract.webhooks import WebhookVerifier


@runtime_checkable
class AppInteractions(Protocol):
    """The interactions namespace (``app.interactions``) — the ``ask_user`` facade."""

    @property
    def ask_user(self) -> AskUser:
        """The bound, :class:`~tai42_contract.interactions.AskUser`-typed ``ask_user`` callable.

        An in-process plugin asks a human without importing the skeleton:
        ``await tai42_app.interactions.ask_user(question, ..., mode="async",
        expiry_at=...)``. The return shape is the ``AskUser`` contract's:
        ``mode="sync"`` returns the typed answer, ``mode="async"`` returns a
        ``SuspendedInteraction``. Its full call signature is the ``AskUser``
        Protocol's, including the per-ask ``on_mismatch`` digression policy and
        ``mismatch_notice`` retry text a channel-delivered ask carries. A facade
        EXPOSURE of the skeleton helper through the already-typed Protocol — no new
        ask semantics.
        """
        ...


@runtime_checkable
class AppWebhookVerifiers(Protocol):
    """The webhook-verifier registry namespace (``app.webhook_verifiers``)."""

    def register(self, name: str, verifier: WebhookVerifier) -> None:
        """Register a :class:`WebhookVerifier` under ``name``.

        A provider plugin calls this through the ``tai42_app`` handle when its
        import-only ``webhook_verifier_modules`` entry loads. Registering a name
        already taken raises loudly — a silent overwrite could swap a topic's
        verifier out from under a live binding.
        """
        ...

    def get(self, name: str) -> WebhookVerifier:
        """Fetch a registered verifier by name; raise loudly on an unknown name.

        Resolution happens when a verifier is bound to a public webhook door, so
        an unknown name surfaces at bind time as a loud failure, never a
        silently-unverified door.
        """
        ...


@runtime_checkable
class AppChannels(Protocol):
    """The channel registry namespace (``app.channels``) and inbound-answer ladder."""

    def register(self, name: str, channel: Channel) -> None:
        """Register a :class:`Channel` under ``name``.

        A channel plugin calls this through the ``tai42_app`` handle when its
        import-only ``channel_modules`` entry loads. Registering a name already
        taken raises loudly — a silent overwrite could swap the medium a live
        ask is delivered on.
        """
        ...

    def get(self, name: str) -> Channel:
        """Fetch a registered channel by name; raise loudly on an unknown name.

        Resolution happens when ``ask_user`` is called with ``channel=name``,
        BEFORE any interaction state is written, so an unknown name surfaces as
        a loud failure, never a question silently delivered nowhere.
        """
        ...

    def names(self) -> list[str]:
        """Every registered channel name, for the channels catalog route."""
        ...

    async def handle_inbound_answer(
        self,
        *,
        channel_id: str,
        correlation_key: str,
        answer: Any,
        store: CorrelationStore,
        bridge: InboundBridge,
    ) -> InboundAnswerResult:
        """Resolve one inbound participant reply against its pending ask.

        This is the ONE shared inbound-answer ladder every correlated channel calls
        instead of hand-rolling its own "forward → interpret 2xx/404/400 →
        release/bridge/keep" sequence.

        Returns an :class:`InboundAnswerResult`: the ``outcome`` the channel maps to its
        transport ack, plus the door's ``retry_reason``/``retry_field`` when it rejected
        the answer's content, so a channel that owns its correction surface can render the
        door's specific message.

        A channel computes its own opaque ``correlation_key`` for the participant's address,
        provides the ``answer`` value to forward to the door, its
        :class:`~tai42_contract.channels.CorrelationStore`, and an :class:`InboundBridge` of the
        fields a bridged turn needs.

        SEAM SYMMETRY (``bridge.params`` ↔ answer params): the ``InboundBridge`` may carry opaque
        channel enrichment in ``params`` — the answer-path counterpart of a conversation entry's
        ``params``. The ladder threads it BOTH ways so enrichment is never dropped on either arm:
        the answer is forwarded to the ask's callback door as ``{"answer": answer, "params":
        params}`` (params present only when set), landing on
        :attr:`~tai42_contract.interactions.models.InteractionResponse.params` for the asking flow
        to read beside ``answer``; and on the BRIDGE arm the same ``params`` are passed to
        ``accept`` as its entry ``params``. The ladder:

        * No pending ask on the key -> :attr:`InboundAnswerOutcome.NO_CORRELATION`
          with NO side effect: the CALLER bridges the reply as a normal turn (the
          ladder never bridges on a miss, exactly as each channel did by hand).
        * The door accepts (2xx) -> release the correlation, return
          :attr:`InboundAnswerOutcome.FORWARDED`.
        * The door returns 404 (the ask was withdrawn/expired/cancelled) -> release,
          bridge the reply as a fresh turn, return :attr:`InboundAnswerOutcome.BRIDGED`.
        * The door returns 400 on a LIVE ask -> read the door's ``retry_in_place``
          (default True): True keeps the correlation, notifies the participant what's
          expected, fires ONE operator event, returns :attr:`InboundAnswerOutcome.RETRY_KEPT`;
          False (a hard mismatch) releases, notifies the participant the question is closed,
          fires the event, bridges the reply, returns :attr:`InboundAnswerOutcome.BRIDGED`.
        * Anything else (401/413/5xx, or a transport fault) -> do NOT release; raise
          :class:`AnswerForwardError` so the channel's webhook redelivery re-runs the
          ladder. The answer is never silently lost.

        The callback host is pinned to the platform's configured public base before the
        forward; a stored callback whose host does not match is released and treated as
        :attr:`InboundAnswerOutcome.NO_CORRELATION`.
        """
        ...

    async def record_send_receipt(
        self, channel: str, provider_message_id: str, status: DeliveryReceipt, *, errors: Any = None
    ) -> bool:
        """Post an out-of-band delivery receipt for a ``notify_user`` send back onto its originating trace.

        The send is a ``notify_user`` channel send with no conversation record. The tier-2
        counterpart to the conversation bridge's ``record_delivery_status``: a
        channel's delivery-status webhook calls this when the bridge does not own the
        outbound id (``record_delivery_status`` raised ``LookupError``). It resolves the id
        through the TTL'd send-receipt index and, on a hit, emits a ``delivery_receipt`` event
        into the recorded trace (a FAILED receipt at ERROR level), so the originating trace
        shows delivered-vs-accepted for its sends. ``errors`` carries any provider error
        detail for the event input.

        Returns ``True`` when the id was a known send (event emitted), ``False`` when it
        is not — so the webhook keeps its genuinely-unknown-id log only on a ``False``. A
        no-op returning ``False`` where the send-receipt store is unconfigured.
        """
        ...


class PendingMessage(BaseModel):
    """One participant message accepted on a thread but not yet carried into a turn.

    The projection :meth:`AppConversations.pending_messages` returns, so a body running inside a
    turn can learn a newer message is waiting and stop before an irreversible step. ``message_id``
    is the accepted record's id, ``text`` its verbatim inbound text, ``accepted_at`` the epoch
    seconds it was accepted at (the thread index's own score). Frozen.
    """

    model_config = ConfigDict(frozen=True)

    message_id: str = Field(min_length=1)
    text: str
    accepted_at: float


@runtime_checkable
class AppConversations(Protocol):
    """The conversation bridge's entry surface for medium adapters.

    Inbound messages arrive via ``accept``, out-of-band delivery receipts via
    ``record_delivery_status``. Routing-row CRUD, the turn engine and the API
    door are not part of this facet.
    """

    async def accept(
        self,
        channel: str,
        our_identity: str,
        client_address: str,
        cap_key: str,
        text: str,
        provider_message_id: str,
        params: dict[str, str] | None = None,
        form: dict[str, Any] | None = None,
        attachments: list[MediaItem] | None = None,
        location: LocationElement | None = None,
        locale: str | None = None,
    ) -> str:
        """Accept one inbound channel message, persist it, and return its ``message_id`` (a uuid4).

        Routed to the ``door=channel`` route matching ``(channel, our_identity)``; the
        turn runs as that route's execution key and answers back over the same channel.
        Idempotent on ``(channel, provider_message_id)`` — a redelivery returns the
        existing ``message_id`` and starts no second turn. Raises when no route matches.

        ``client_address`` is the conversation identity (its thread and transcript).
        ``cap_key`` is the party the per-address turn cap holds accountable, which the
        door composes: a provider channel passes its attested address (the two match),
        a door that mints its own visitor id passes a key the platform does NOT mint —
        its network client bucket — so the cap bounds spend the visitor cannot reset.
        Required and non-blank; a door that omits it is refused, never defaulted.

        ``params`` are optional entry parameters the door captured with the conversation
        entry, delivered verbatim to a tool target's payload under ``params``;
        ``None``/empty leaves the payload unchanged. The door validates them with
        :func:`~tai42_contract.conversations.validate_entry_params` before accept.
        ``params`` is ALSO the opaque-enrichment seam for the channel-specific inbound
        context that has no shared shape — a tapped reply/list id, a template button
        payload, a referral, the reply-to context of the message the participant quoted, a
        contact card or a reaction — which a channel adapter encodes as string entries a
        route reads deliberately; the platform adds no typed field for those.

        ``form`` is an optional structured participant submission (an ask-less form's answers)
        riding WITH the required rendered ``text`` — the text stays the whole turn every
        consumer sees, while a tool target's ``payload_expr`` may map the structured copy
        from the payload's ``form`` key (present only when the inbound carried one). The
        platform bounds it as pure transport
        (:func:`~tai42_contract.conversations.validate_inbound_form`) and attaches no
        meaning and NO TRUST to the contents: participant-shaped data, never schema-conformant.

        ``attachments`` is the STRUCTURED media the participant sent WITH the message — the
        inbound counterpart of an outbound answer's ``media`` — as :class:`MediaItem`
        (image/document/video/audio; a channel resolves a sticker to image/video, a voice
        note to audio). ``location`` is a :class:`LocationElement` the participant shared. Both
        are machine-consumable content with a shared cross-channel shape, so they are typed
        fields (not ``params``): they land in a tool target's payload under the stable
        ``attachments`` / ``location`` keys, present only when the inbound carried them, so
        a form-/media-unaware target still sees the whole turn as ``text``. ``None`` leaves
        the payload unchanged.

        ``locale`` is the participant's BCP 47 language tag the channel resolved from its native
        inbound (a per-message language hint), captured onto the turn's subject so the
        rendering layer resolves every text template and list format against it — flows and
        state templates never select a language. It seeds a first-contact person's stored
        locale and, absent a stored operator override, is the turn's resolved locale;
        ``None`` means the channel supplied none (no silent default to any language).
        """
        ...

    async def record_delivery_status(self, channel: str, provider_message_id: str, status: DeliveryReceipt) -> None:
        """Ingest a channel's out-of-band delivery receipt for an outbound message.

        ``provider_message_id`` is one of the ids a prior ``notify`` returned, resolved
        to the answer record through the outbound-id reverse index. ``FAILED`` marks the
        record failed; ``DELIVERED`` confirms a ``provisional`` record. Raises when the
        id resolves to no record.
        """
        ...

    async def pending_messages(self, thread_id: str, *, after: str) -> list[PendingMessage]:
        """The thread's participant messages accepted after ``after`` and not yet carried into a turn.

        The ``accepted`` records with ``origin="client"`` and ``inbound_kind="message"`` on
        ``thread_id`` whose acceptance follows the ``after`` message, in acceptance order, each a
        :class:`PendingMessage`. Read-only and in-process (it reads the same thread index the turn
        engine batches over), bounded by the thread's FIFO depth.

        A body running inside a turn asks this — with ``after`` the turn's own lead ``message_id`` —
        to learn whether a newer message is waiting, so it can yield (raise
        :class:`~tai42_contract.conversations.TurnSupersededError`) before an expensive or
        irreversible step. Outside a turn there is nothing pending by definition. An unknown
        ``thread_id`` reads as an empty list; a record already left ``accepted`` is not pending.
        """
        ...

    def register_target_validator(self, target_kind: ConversationTargetKind, validator: TargetBindValidator) -> None:
        """Register a bind validator for routes whose target is of ``target_kind``.

        A plugin calls this through the ``tai42_app`` handle when its module loads.
        Route creation consults every registered validator for the route's target kind
        AFTER the target exists but BEFORE the route is written; a validator returning
        any message lines refuses the creation with them (a 422), so a defect the target
        carries — a flow reading a state no binding supplies — is caught at bind, never
        deferred to run time. Registering two validators for one kind raises loudly.
        """
        ...
