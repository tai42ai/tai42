"""The conversation routing row: the create body, the stored row, and the door/mode vocabulary."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from tai42_contract.conversation_target import ConversationTargetKind
from tai42_contract.conversations.overlap import OverlapPolicy
from tai42_contract.interactions.door_contract import PARKED_VARIABLE_ANNOTATION, ParkableDoorMixin
from tai42_contract.locale import normalize_optional_locale
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, TemplatedText, expression_annotation

#: Which door a route is reached through: ``api`` delivers by signed callback,
#: ``channel`` delivers back through the medium adapter's ``notify``.
ConversationDoor = Literal["api", "channel"]

#: A per-target-kind bind validator registered on the conversations facet: given a route's
#: FULL create model, returns the BLOCKING message lines that forbid binding its target to
#: the route (empty = allow). Route creation consults the registered validator for the
#: target's kind before the route exists, so a defect the target carries against the route's
#: own door fields — a flow reading a state no binding supplies, an asking agent with no
#: reply/resume path — is refused at bind, not discovered at run time. Passing the whole
#: model (not just the name) lets a validator judge the target against those door fields.
#: Blocking only — a warning is not a bind-path concept.
TargetBindValidator = Callable[["ConversationRouteCreate"], Awaitable[Sequence[str]]]

#: A thread's conversation control mode: ``agent`` runs the target turn (an agent run or a
#: tool dispatch); ``manual`` suppresses the target turn so an operator answers by hand,
#: while platform control turns (pairing, first-contact greeting) still run.
ConversationMode = Literal["agent", "manual"]

#: The mode values, for the store and the doors that validate an override.
CONVERSATION_MODES: tuple[ConversationMode, ...] = ("agent", "manual")

# Route names key the ``bridge:{route_name}:{address}`` thread namespace, so the
# vocabulary must exclude ``:``.
ROUTE_NAME_RE = re.compile(r"^[a-z0-9-]+$")


def _is_https_url(value: str) -> bool:
    # Must parse (not ``startswith``): rejects hostless ``https://``, scheme-only or
    # relative strings, ``user@host`` authority spoofing, and malformed authorities.
    try:
        split = urlsplit(value)
    except ValueError:
        return False
    return split.scheme == "https" and bool(split.hostname) and "@" not in split.netloc


class ConversationRouteCreate(ParkableDoorMixin):
    """The client-facing create/edit body for a conversation route: the fields a caller supplies.

    Binds a ``(target_kind, target_name)`` — an ``agent`` run or a ``tool`` dispatch — to
    an ``execution_key`` the turn runs AS (bound with pass-role at create). Both target kinds
    may carry the parkable-door jq (:class:`ParkableDoorMixin`: ``cancel_expr`` / ``resume_expr`` /
    ``start_expr`` / ``extras_expr``) plus a ``reply_expr`` (jq mapping the started run's result to
    the reply): ``start_expr`` builds the run's kwargs (a tool dispatch's kwargs or an agent run's
    ``astream`` kwargs), the cancel/resume jqs act on the run's parked interactions, and
    ``reply_expr`` maps the terminal to the participant reply. ``api`` rows MAY carry an https
    ``callback_url`` (the answer sink; when absent
    the caller reads its answer back from the poll door); ``channel`` rows carry the
    registry ``channel`` plus the ``our_identity`` the medium is texted at (N rows may
    share a channel, each its own identity). The server-derived ``callback_secret`` and
    ``execution_key_fingerprint`` are deliberately absent; :class:`ConversationRoute` is
    this shape plus those. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    # A ``:``-free slug: it keys the ``bridge:{route_name}:{client_address}`` thread
    # namespace, where a ``:`` would let one route's threads collide with another's.
    route_name: str
    door: ConversationDoor
    target_kind: ConversationTargetKind
    target_name: str = Field(min_length=1)
    # A templated text carrying (inline or by stored id) a jq program mapping the inbound turn
    # payload to the started run's kwargs, overriding the mixin's generic ``start_expr`` gloss with
    # this route's own input keys. Rendered then compiled at create. ``reply_expr`` (below) maps the
    # SUCCESS shape: a result whose own ``status`` names a non-success terminal diverts to the turn's
    # error outcome without being mapped. Both carry the ``x-tai42-expression`` schema annotation
    # (via ``Annotated`` so the attribute default stays the ``None`` literal — the api-gate flags a
    # ``Field(default=...)`` redeclaration as breaking) so a schema-driven UI auto-renders the jq
    # editor. The four door jqs read the run's currently parked interactions as ``$parked``.
    start_expr: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="start expression",
                    blurb="the inbound turn payload the route maps to the started run's kwargs",
                    variables=[PARKED_VARIABLE_ANNOTATION],
                    keys=[
                        ("message", "the inbound message text"),
                        ("sender", "the sending address"),
                        ("our_identity", "door=channel: the medium address we are texted at"),
                        ("channel", "door=channel: the registry channel name"),
                        ("thread_id", "the turn's canonical thread id (the thread doors' id)"),
                        ("person_id", "multichannel only: the linked person's id"),
                        ("person_addresses", "multichannel only: the person's known addresses"),
                        ("params", "non-empty entry params, nested under this key"),
                        ("form", "a structured form submission, present only when the inbound carried one"),
                        (
                            "attachments",
                            "inbound media the participant sent, present only when the inbound carried some",
                        ),
                        (
                            "location",
                            "a geographic point the participant shared, present only when the inbound carried one",
                        ),
                        (
                            "messages",
                            "overlap deliver=all: the batch carried into this turn, in acceptance order, "
                            "each {id, text, accepted_at} (+ form/attachments/location when that message carried them)",
                        ),
                        (
                            "superseded",
                            "overlap deliver=all: messages dropped in favour of this turn, in the same shape; "
                            "present only when non-empty",
                        ),
                        ("turn", "the turn ids: {id, inbound: {id, kind, source}}"),
                        ("event", "an event turn's structured payload {id, kind, payload}; absent on a message turn"),
                    ],
                    returns="the JSON object dispatched as the started run's kwargs; null starts nothing",
                )
            }
        ),
    ] = None
    reply_expr: Annotated[
        TemplatedText | None,
        Field(
            json_schema_extra={
                EXPRESSION_ANNOTATION_KEY: expression_annotation(
                    label="reply expression",
                    blurb=(
                        "the started run's result (the SUCCESS shape) the route maps to the participant reply; "
                        "null when the run asked its caller instead of finishing"
                    ),
                    variables=[
                        (
                            "turn",
                            "the turn ids and subject this reply answers ({id, inbound, subject}); null when an "
                            "out-of-band delivery's originating record has aged out",
                            {"id": "m-1", "inbound": {"id": "i-1", "kind": "message", "source": "api"}},
                        ),
                        (
                            "asks",
                            "the run's caller-ask entries when it asked instead of finishing (each the full parked "
                            "entry); an empty list on a plain result",
                            [{"id": "i-42", "question": "proceed?", "answer_format": "confirm"}],
                        ),
                        PARKED_VARIABLE_ANNOTATION,
                    ],
                    returns="the reply: null (silent), a string, or a list of answer parts",
                )
            }
        ),
    ] = None
    # The thread's control mode when no per-thread override is set: ``agent`` runs the
    # target turn, ``manual`` suppresses it for an operator to answer.
    initial_mode: ConversationMode = "agent"
    execution_key: str = Field(
        min_length=1,
        description=(
            "The api-key ``user_id`` the turn runs AS; its live stored grants authorize "
            "the agent run or tool dispatch and every tool call the turn makes."
        ),
    )
    channel: str | None = None  # door=channel: the registry name, ``:``-free
    our_identity: str | None = None  # door=channel: the medium address we are texted at
    callback_url: str | None = None  # door=api: the optional https answer sink; absent = poll-only
    # A per-route override of the global ``per_address_turns_per_hour`` cap: the positive
    # per-hour turn rate this route's per-address buckets run at, or ``None`` to run at the
    # global rate.
    turns_per_hour_override: int | None = Field(default=None, gt=0)
    # The participant-facing reply sent when a conversational turn on this route fails; ``None`` uses
    # the built-in English default. LITERAL text — no placeholders/templating. Non-blank when
    # set. (No other text field in this file carries a max_length; 2000 is a defensible bound
    # for a single participant-facing reply.)
    error_reply_text: str | None = Field(default=None, min_length=1, max_length=2000)
    # The operator-declared default locale for templated reply parts when the turn states none;
    # the last fallback under a per-turn or stored-contact locale, canonicalized through the one
    # locale seam (a malformed tag is rejected loudly, never guessed). ``None`` declares no
    # route default.
    locale: str | None = None
    # How this route treats a running turn and the newer participant messages that overlap it
    # (learn/cancel/carry). The default policy (continue/one/no window) runs one turn per
    # message and leaves the payload unchanged.
    overlap: OverlapPolicy = OverlapPolicy()

    @field_validator("route_name")
    @classmethod
    def _check_route_name(cls, value: str) -> str:
        if not ROUTE_NAME_RE.fullmatch(value):
            raise ValueError(f"route_name must be a slug matching {ROUTE_NAME_RE.pattern!r}: {value!r}")
        return value

    @field_validator("error_reply_text")
    @classmethod
    def _check_error_reply_text(cls, value: str | None) -> str | None:
        # A set override must be non-blank: a whitespace-only reply would deliver an empty
        # participant-facing message where the built-in default was intended.
        if value is not None and not value.strip():
            raise ValueError("error_reply_text must be non-blank when set")
        return value

    @field_validator("locale")
    @classmethod
    def _canonical_locale(cls, value: str | None) -> str | None:
        return normalize_optional_locale(value)

    @model_validator(mode="after")
    def _check_door_fields(self) -> ConversationRouteCreate:
        if self.door == "channel":
            if not (self.channel and self.channel.strip()):
                raise ValueError("door=channel requires a non-blank channel")
            if ":" in self.channel:
                # The channel name prefixes the inbound-dedupe and outbound-index keys, so
                # a ``:`` in it shifts the separator and lets one channel read another's.
                raise ValueError(f"door=channel requires a channel name free of ':': {self.channel!r}")
            if not (self.our_identity and self.our_identity.strip()):
                raise ValueError("door=channel requires a non-blank our_identity")
            if self.callback_url is not None:
                raise ValueError("door=channel carries no callback_url")
        else:
            if self.callback_url is not None and not _is_https_url(self.callback_url):
                raise ValueError("door=api callback_url must be an absolute https url when set")
            if self.channel is not None:
                raise ValueError("door=api carries no channel")
            if self.our_identity is not None:
                raise ValueError("door=api carries no our_identity")
        return self


class ConversationRoute(ConversationRouteCreate):
    """The stored routing row: :class:`ConversationRouteCreate` plus the two server-derived fields.

    What the manager persists and backup restore validates.

    ``callback_secret`` (present only on an ``api`` row that declares a ``callback_url``)
    signs the delivery callback; it is excluded from export and re-minted per row on import,
    so callbacks signed with the pre-import secret no longer verify.
    ``execution_key_fingerprint`` is the bound key's per-mint
    identity: a turn matches it against the live key, so a revoke+remint of the same
    ``user_id`` fails closed.
    """

    callback_secret: str | None = None
    execution_key_fingerprint: str = Field(
        min_length=1,
        description=(
            "The bound key's per-mint identity the turn resolves against, derived "
            "server-side at create and never client-supplied."
        ),
    )
