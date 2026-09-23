"""Human-messaging facets: webhook verifiers, channels, conversations, and the ask facade."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from tai42_contract.channels import Channel, CorrelationStore, InboundAnswerResult, InboundBridge
from tai42_contract.conversations import ConversationTargetKind, DeliveryReceipt, TargetBindValidator
from tai42_contract.interactions.answer_check import QuestionFormat
from tai42_contract.interactions.asker import Ask
from tai42_contract.interactions.models import LocationElement, MediaItem
from tai42_contract.interactions.visit import ParkedEntry, Visit, VisitOutcome
from tai42_contract.states import StateContext
from tai42_contract.webhooks import WebhookVerifier


@runtime_checkable
class AppInteractions(Protocol):
    """The interactions namespace (``app.interactions``) — the ``ask`` facade and answer check."""

    def check_answer(self, question: QuestionFormat, answer: Any) -> None:
        """Validate ``answer`` against ``question`` — the ONE answer check every door reaches.

        ``question`` is a :class:`~tai42_contract.interactions.QuestionFormat` (the answer
        format plus its payload — no stored request needed). Returns ``None`` when the answer
        conforms; raises :class:`~tai42_contract.interactions.AnswerMismatchError` (carrying the
        failing field's dotted path when the fault locates to one) otherwise. The answer door,
        the callback door, a resumed run's ``visit`` and plugins all validate through this one
        callable so every surface applies identical rules per format.
        """
        ...

    async def assert_resume_authorized(self, interaction_id: str) -> None:
        """Authorise the caller to resume (or deliver an outcome for) ``interaction_id`` — or raise.

        A driver's continuation face and cross-driver chain-delivery tool call this with the id
        they are about to resume/deliver-to, BEFORE producing any outcome, because those tools are
        dispatchable by name at the run-tool door and the MCP edge. Authorised only inside the
        platform's own resume of that run: it passes iff the ambient run-authorization context
        names ``interaction_id`` directly (the resume origin) OR the interaction's stored run
        delivery identity matches the ambient one (a cross-driver chain re-entry of the same run).
        Raises :class:`~tai42_contract.interactions.ParkResumeUnauthorizedError` for an external
        caller (no origin) or an id belonging to another run — never a mere presence test.
        """
        ...

    def assert_delivery_authorized(self, completion_id: str | None) -> None:
        """Authorise a door delivery-address tool to fire for ``completion_id`` — or raise.

        A door's out-of-band delivery address is a registered tool the platform's delivery ladder
        fires; the tool calls this at entry with the payload's ``completion_id`` before delivering
        anything. It passes iff the ambient delivery-fire context is set AND equals
        ``completion_id`` — a ``None`` id and a call outside any fire never pass. Raises
        :class:`~tai42_contract.interactions.ParkDeliveryUnauthorizedError` otherwise, so a caller
        naming the address tool directly at the run-tool door or the MCP edge delivers nothing.
        """
        ...

    def redelivery_horizon_seconds(self) -> int:
        """The platform's redelivery retention horizon in seconds.

        The continuation-due record ages out at it and the reaper caps its redelivery backoff at
        it, so no resume redelivery fires past it. A resuming driver derives its own
        resolution-record retention from this value rather than a hard-coded guess.
        """
        ...

    @property
    def visit(self) -> Visit:
        """The bound, :class:`~tai42_contract.interactions.Visit`-typed shared-visit callable.

        The ONE seam every door drives a parkable run through: it runs the cancel / resume / take /
        start order once for every caller (checks everything before doing anything, cancels,
        resumes or takes at most one action besides cancel, starts the target when nothing was
        resumed, normalises what came back), and returns a
        :class:`~tai42_contract.interactions.VisitOutcome`. A door resolves its own jqs to plain
        values and hands them in; the generic ``list_parked`` / ``resume_parked`` / ``cancel_parked``
        below are thin wrappers over this same seam.
        """
        ...

    def park_answer(self, outcome: VisitOutcome) -> Any:
        """The ONE park-answer shape a direct door hands back for a :class:`VisitOutcome`.

        A plain ``result`` is the tool's own value; an ``asks`` outcome is the caller ask entries
        the run parked (``{"asks": [entry, ...]}``); a ``parked`` outcome is the suspended
        sentinel; ``none`` is ``None``. Both the synchronous run-tool door and the background
        submit's terminal record shape a park through this ONE callable, so a poller of either
        door reads identical bytes and can tell a caller-ask park (answerable through
        ``resume_parked``) from a user-ask park. It never reveals a wrapped secret — the sync door
        reveals those on the ``result`` kind itself and every recorder masks its own copy.
        """
        ...

    async def normalise_started(self, value: Any) -> VisitOutcome:
        """Classify a raw start return into a ``started`` :class:`VisitOutcome` over the ambient subject.

        The same normalisation :meth:`visit` applies to what its ``start`` returned — caller asks,
        a re-park, or a final result — exposed for a door that already ran its start INSIDE its own
        :meth:`visit` (a hook fire whose visit owns the run) and only needs the return classified,
        so its record carries the same park answer both direct doors return.
        """
        ...

    async def list_parked(self) -> list[ParkedEntry]:
        """Every parked interaction on the current run's subject — the full parked entries.

        Reads the ambient run's subject candidates and returns the union over its subject keys,
        de-duplicated, each a :class:`~tai42_contract.interactions.ParkedEntry` with its id, status
        (``asking``/``running``/``finished``/``failed``) and the question/answer fields a
        state-backed entry carries. The same rows a door-contract jq reads as ``$parked``.
        """
        ...

    async def list_parked_for(self, context: StateContext | None) -> list[ParkedEntry]:
        """Every parked interaction on ``context``'s subject — the same entries as :meth:`list_parked`.

        A parkable-driving door fetches this over its OWN
        :class:`~tai42_contract.states.StateContext` once and feeds the list to the door-contract
        evaluator as ``$parked``, keeping the contract evaluation a pure function of an injected
        list rather than an ambient read. A ``None`` context has no subject and returns an empty
        list.
        """
        ...

    def current_fire_identity(self) -> tuple[str, str] | None:
        """The ambient execution identity as the ``(user_id, fingerprint)`` pair a fire forwards, or ``None``.

        A background task tool dispatched from within a door fire forwards this pair onto its worker
        job so the deferred fire re-binds the SAME authority; ``None`` when no identity (or no
        per-mint fingerprint) is bound, so nothing is forwarded and the deferred fire fails closed on
        any credential seam rather than under a substituted principal.
        """
        ...

    def bound_execution_identity_for_fire(
        self, execution_key: str, fingerprint: str
    ) -> AbstractAsyncContextManager[Any]:
        """An ``async with`` binding ``execution_key``'s live-grant identity for a receiver-less door fire.

        A scheduled fire has no live caller, so the door stores the firing identity (the execution
        key's ``user_id`` and its per-mint ``fingerprint``) at create and binds it here at fire, so a
        run that async-parks can rebind its continuation and a ``to="caller"`` resume acts under the
        same authority. Homed beside the door's other kit-reachable seams so a backend plugin binds a
        scheduled fire's identity without importing the skeleton. Raises when the key no longer
        carries authority — the fire fails closed, never under a substituted principal.
        """
        ...

    async def resume_parked(self, interaction_id: str, payload: Any = ...) -> VisitOutcome:
        """Resume a parked caller ask with ``payload``, or TAKE its waiting outcome when omitted.

        With ``payload`` given it resumes the ``asking`` caller ask ``interaction_id`` and returns
        the resumed run's normalised outcome; with ``payload`` omitted it TAKES a ``finished``
        entry's waiting outcome (``kind="result"``) or re-raises a ``failed`` one as
        :class:`~tai42_contract.interactions.ParkableRunFailedError`. A live inline receiver, so the
        resumed run's terminal is returned inline rather than delivered out of band.
        """
        ...

    async def cancel_parked(self, ids: list[str]) -> VisitOutcome:
        """Whole-chain kill every parked interaction named in ``ids`` on the current run's subject.

        Each id is torn down through the one teardown seam (its run and every run it linked above it
        close for good, delivering FAILED once to a door that started it); returns a
        :class:`~tai42_contract.interactions.VisitOutcome` naming the ids cancelled. An id outside
        the run's own parked list raises :class:`~tai42_contract.interactions.ParkedEntryGoneError`
        with nothing cancelled.
        """
        ...

    @property
    def ask(self) -> Ask:
        """The bound, :class:`~tai42_contract.interactions.Ask`-typed ``ask`` callable.

        An in-process plugin asks a human without importing the skeleton:
        ``await tai42_app.interactions.ask(question, ..., mode="async",
        expiry_at=...)``. The return shape is the ``Ask`` contract's:
        ``mode="sync"`` returns the typed answer, ``mode="async"`` returns a
        ``SuspendedInteraction``. Its full call signature is the ``Ask``
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

        Resolution happens when ``ask`` is called with ``channel=name``,
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
        consumer sees, while a tool target's ``start_expr`` may map the structured copy
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
