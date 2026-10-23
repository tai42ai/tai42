"""The conversation routing row: the create body, the stored row, and the door/mode vocabulary."""

from __future__ import annotations

import re
import string
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Annotated, Final, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from tai42_contract.conversation_target import ConversationTargetKind
from tai42_contract.conversations.overlap import OverlapPolicy
from tai42_contract.interactions.door_contract import PARKED_VARIABLE_ANNOTATION, ParkableDoorMixin
from tai42_contract.interactions.reply_text import NON_BLANK_PATTERN, ReplyText
from tai42_contract.locale import normalize_optional_locale
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, TemplatedText, expression_annotation

if TYPE_CHECKING:
    from tai42_contract.presets import PresetBody

#: Which door a route is reached through: ``api`` delivers by signed callback,
#: ``channel`` delivers back through the medium adapter's ``notify``.
ConversationDoor = Literal["api", "channel"]

#: A bind validator a plugin registers for the one tool or agent it OWNS, consulted when a
#: conversation route's target RESOLVES to that owner (an agent is its own name; a tool walks
#: its parent-tool chain, so a preset inherits its base tool's owner). Given the route's FULL
#: create model and the target's CANDIDATE body — the owned tool's stored body for a preset
#: target, or ``None`` when the target carries no such body — it returns the BLOCKING message
#: lines that forbid binding the target to the route (empty = allow). Consulted after the
#: target exists but before the row is written, so a defect the target carries against the
#: route's own door fields is refused at bind, not discovered at run time. The candidate body
#: lets the owner judge an UNSAVED version of the target (a version a write has not yet
#: committed), not only the active one. Passing the whole create model (not just the name) lets
#: the owner judge the target against those door fields. Blocking only — a warning is not a
#: bind-path concept.
TargetBindValidator = Callable[
    ["ApiConversationRouteCreate | ChannelConversationRouteCreate", "PresetBody | None"], Awaitable[Sequence[str]]
]

#: A thread's conversation control mode: ``agent`` runs the target turn (an agent run or a
#: tool dispatch); ``manual`` suppresses the target turn so an operator answers by hand,
#: while platform control turns (pairing, first-contact greeting) still run.
ConversationMode = Literal["agent", "manual"]

#: The mode values, for the store and the doors that validate an override.
CONVERSATION_MODES: tuple[ConversationMode, ...] = ("agent", "manual")

# Route names key the ``bridge:{route_name}:{address}`` thread namespace, so the
# vocabulary must exclude ``:``.
ROUTE_NAME_RE = re.compile(r"^[a-z0-9-]+$")

#: An absolute https URL (scheme case-insensitive) whose authority is non-empty and carries no
#: ``@`` userinfo. It never refuses a value :func:`_require_https_url` accepts; that check stays the
#: authority for what the pattern cannot express (an empty hostname, a malformed bracket host, a
#: whitespace or control character anywhere).
HTTPS_CALLBACK_PATTERN: Final = r"^[hH][tT][tT][pP][sS]://[^/?#@]+(?:[/?#]|$)"

#: A channel name: non-blank and ``:``-free. It prefixes the inbound-dedupe and outbound-index
#: keys, so a ``:`` in it would shift the separator and let one channel read another's entry.
CHANNEL_NAME_PATTERN: Final = r"^[^:]*[^:\s][^:]*$"


def _require_https_url(value: str) -> str:
    # Must parse (not ``startswith``): rejects hostless ``https://``, scheme-only or
    # relative strings, ``user@host`` authority spoofing, and malformed authorities.
    # ``urlsplit`` silently strips leading whitespace/control characters and drops tab/CR/LF,
    # so a value carrying any is refused before parsing: the stored URL is the one validated.
    clean = not any(ch.isspace() or not ch.isprintable() for ch in value)
    try:
        split = urlsplit(value) if clean else None
    except ValueError:
        split = None
    if split is None or split.scheme != "https" or not split.hostname or "@" in split.netloc:
        raise ValueError("callback_url must be an absolute https URL with no userinfo")
    return value


# The fields a route's ``pairing_code`` reply may reference: the fresh code and its expiry.
_PAIRING_CODE_FIELDS: Final = frozenset({"pairing_code", "expires_at"})


def _check_pairing_code_template(template: str) -> str:
    # Parsed exactly as :meth:`str.format` renders it, so a malformed template, an auto-numbered
    # ``{}``, a foreign field, or a field carrying a conversion or format spec is refused at the
    # write, never rendered wrong or raised when the reply is drawn. The code itself must appear:
    # a code reply without the code tells the end user nothing.
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"reply_texts.pairing_code is not a valid template: {exc}") from exc
    names: set[str] = set()
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in _PAIRING_CODE_FIELDS or conversion is not None or format_spec:
            raise ValueError(
                "reply_texts.pairing_code may reference only the {pairing_code} and {expires_at} fields, "
                f"got an unsupported field {field_name!r}"
            )
        names.add(field_name)
    if "pairing_code" not in names:
        raise ValueError("reply_texts.pairing_code must contain the {pairing_code} field")
    return template


class ReplyTexts(BaseModel):
    """The caller's words for the platform's fixed participant replies on one route. Frozen, strict keys.

    ``error`` is sent when a turn fails and the target gave no reply; ``slow_down`` when an address
    is over the turn cap; ``pairing_linked`` when a pair code was redeemed and the two persons
    merged; ``pairing_unlinked`` when ``/unlink`` detached the address; ``pairing_not_linked`` when
    ``/unlink`` came from an address linked to nothing; ``pairing_code_invalid`` when a redeem was
    refused; ``pairing_code`` is the ``/link`` reply carrying the fresh code, a template that MUST
    reference ``{pairing_code}`` and may reference ``{expires_at}`` (the code's ISO-8601 expiry),
    with no conversion or format spec; ``pairing_link_unavailable`` when a redeem met a person being
    erased (its code is consumed, so the reply asks for a new one); ``pairing_unlink_unavailable``
    when ``/unlink`` met the sender's person being erased; ``rejected_unsupported_type`` / ``rejected_too_large`` /
    ``rejected_could_not_receive`` are the notices for an inbound message the channel cannot
    bridge, a file over the cap, and a file the channel could not fetch. Every member but
    ``pairing_code`` is literal text: a ``{`` in it is drawn as written. Each is a
    :data:`~tai42_contract.interactions.ReplyText` in the line's language; absent, the built-in
    English reply is sent. An empty catalogue names nothing and is refused; an unknown key is
    refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    error: ReplyText | None = None
    slow_down: ReplyText | None = None
    pairing_linked: ReplyText | None = None
    pairing_unlinked: ReplyText | None = None
    pairing_not_linked: ReplyText | None = None
    pairing_code_invalid: ReplyText | None = None
    pairing_code: ReplyText | None = None
    pairing_link_unavailable: ReplyText | None = None
    pairing_unlink_unavailable: ReplyText | None = None
    rejected_unsupported_type: ReplyText | None = None
    rejected_too_large: ReplyText | None = None
    rejected_could_not_receive: ReplyText | None = None

    @field_validator("pairing_code")
    @classmethod
    def _pairing_code_template(cls, value: str | None) -> str | None:
        return None if value is None else _check_pairing_code_template(value)

    @model_validator(mode="after")
    def _at_least_one_set(self) -> ReplyTexts:
        if all(getattr(self, name) is None for name in type(self).model_fields):
            raise ValueError("reply_texts must set at least one member")
        return self


class _ConversationRouteFields(ParkableDoorMixin):
    """The client-facing create/edit body for a conversation route: the fields a caller supplies.

    Binds a ``(target_kind, target_name)`` — an ``agent`` run or a ``tool`` dispatch — to
    an ``execution_key`` the turn runs AS (bound with pass-role at create). Both target kinds
    may carry the parkable-door jq (:class:`ParkableDoorMixin`: ``cancel_expr`` / ``resume_expr`` /
    ``start_expr`` / ``extras_expr``) plus a ``reply_expr`` (jq mapping the started run's result to
    the reply): ``start_expr`` builds the run's kwargs (a tool dispatch's kwargs or an agent run's
    ``astream`` kwargs), the cancel/resume jqs act on the run's parked interactions, and
    ``reply_expr`` maps the terminal to the participant reply. The door variants refine the
    door-exclusive fields: :class:`ApiConversationRouteCreate` MAY carry an https ``callback_url``
    (the answer sink; when absent the caller reads its answer back from the poll door);
    :class:`ChannelConversationRouteCreate` carries the registry ``channel`` plus the
    ``our_identity`` the medium is texted at (N rows may share a channel, each its own identity).
    Each variant keeps every field, a field exclusive to the other door typed ``None``. The
    server-derived ``callback_secret`` and ``execution_key_fingerprint`` are deliberately absent;
    the stored variants are these shapes plus those. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    # A ``:``-free slug: it keys the ``bridge:{route_name}:{client_address}`` thread
    # namespace, where a ``:`` would let one route's threads collide with another's.
    route_name: Annotated[str, Field(pattern=ROUTE_NAME_RE.pattern)]
    target_kind: ConversationTargetKind
    target_name: str = Field(min_length=1)
    # A templated text carrying (inline or by stored id) a jq program mapping the inbound turn
    # payload to the started run's kwargs, overriding the mixin's generic ``start_expr`` gloss with
    # this route's own input keys. Rendered then compiled at create. ``reply_expr`` (below) maps the
    # run's returned value; a failed run raises and takes the error outcome before mapping. Both
    # carry the ``x-tai42-expression`` schema annotation
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
                        ("route", "the name of the conversation route the turn entered through"),
                        ("thread_id", "the turn's canonical thread id (the thread doors' id)"),
                        ("person_id", "multichannel only: the linked person's id"),
                        ("person_addresses", "multichannel only: the person's known addresses"),
                        ("params", "non-empty entry params, nested under this key"),
                        ("form", "a structured form submission, present only when the inbound carried one"),
                        (
                            "form_tag",
                            "the caller-set opaque per-send form tag, present only when the submission carried one",
                        ),
                        (
                            "attachments",
                            "inbound media the participant sent, present only when the inbound carried some",
                        ),
                        (
                            "location",
                            "a geographic point the participant shared, present only when the inbound carried one",
                        ),
                        (
                            "reply_to",
                            "the earlier message this one replies to, {message_id, status, text}; present only when "
                            "the inbound quoted one",
                        ),
                        (
                            "messages",
                            "overlap deliver=all: the batch carried into this turn, in acceptance order, "
                            "each {id, text, accepted_at} (+ form/form_tag/attachments/location/reply_to when that "
                            "message carried them)",
                        ),
                        (
                            "superseded",
                            "overlap deliver=all: messages dropped in favour of this turn, in the same shape; "
                            "present only when non-empty",
                        ),
                        (
                            "turn",
                            "the turn: {id, inbound: {id, kind, source}, subject: {target_kind, target_name, person, "
                            "thread, locale}, reply_window: {state, closes_at}}; reply_window.state is open (a "
                            "free-form reply is allowed until closes_at), closed, or none (no window on this channel "
                            "or address)",
                        ),
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
                            "the turn this reply answers ({id, inbound, subject, reply_window}); reply_window "
                            "{state, closes_at} is read when the turn starts, or at delivery for a late reply: open "
                            "until closes_at, closed, or none; null when an out-of-band delivery's originating record "
                            "has aged out",
                            {
                                "id": "m-1",
                                "inbound": {"id": "i-1", "kind": "message", "source": "chat-line"},
                                "subject": {
                                    "target_kind": "agent",
                                    "target_name": "assistant",
                                    "person": None,
                                    "thread": "t-1",
                                    "locale": None,
                                },
                                "reply_window": {"state": "open", "closes_at": "2026-01-02T09:30:00Z"},
                            },
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
    # A per-route override of the global ``per_address_turns_per_hour`` cap: the positive
    # per-hour turn rate this route's per-address buckets run at, or ``None`` to run at the
    # global rate.
    turns_per_hour_override: int | None = Field(default=None, gt=0)
    # The catalogue of the platform's fixed participant replies on this line, in the line's
    # language; ``None`` or an unset member draws the built-in English. A member whose moment
    # never comes on this line (a pairing member on a route whose target does not pair) is never
    # drawn.
    reply_texts: ReplyTexts | None = None
    # The operator-declared default locale for templated reply parts when the turn states none;
    # the last fallback under a per-turn or stored-contact locale, canonicalized through the one
    # locale seam (a malformed tag is rejected loudly, never guessed). ``None`` declares no
    # route default.
    locale: str | None = None
    # How this route treats a running turn and the newer participant messages that overlap it
    # (learn/cancel/carry). The default policy (continue/one/no window) runs one turn per
    # message and leaves the payload unchanged.
    overlap: OverlapPolicy = OverlapPolicy()

    @field_validator("locale")
    @classmethod
    def _canonical_locale(cls, value: str | None) -> str | None:
        return normalize_optional_locale(value)


class ApiConversationRouteCreate(_ConversationRouteFields):
    """An ``api`` route: answers by signed callback to ``callback_url`` when set, else by the poll door."""

    door: Literal["api"]
    channel: None = None
    our_identity: None = None
    callback_url: Annotated[str, Field(pattern=HTTPS_CALLBACK_PATTERN), AfterValidator(_require_https_url)] | None = (
        None
    )


class ChannelConversationRouteCreate(_ConversationRouteFields):
    """A ``channel`` route: answers through the registry ``channel`` at the ``our_identity`` address."""

    door: Literal["channel"]
    # The registry channel name, and the medium address we are texted at.
    channel: Annotated[str, Field(pattern=CHANNEL_NAME_PATTERN)]
    our_identity: Annotated[str, Field(pattern=NON_BLANK_PATTERN)]
    callback_url: None = None


#: The create/edit body of a conversation route, discriminated on ``door``.
ConversationRouteCreate = Annotated[
    ApiConversationRouteCreate | ChannelConversationRouteCreate, Field(discriminator="door")
]


class ConversationRouteCreateBody(RootModel[ConversationRouteCreate]):
    """The published, validating form of a route create: ``.root`` is the door's variant."""


class ApiConversationRoute(ApiConversationRouteCreate):
    """The stored ``api`` routing row: the create shape plus the two server-derived fields.

    ``callback_secret`` (present only when the row declares a ``callback_url``) signs the
    delivery callback; it is excluded from export and re-minted per row on import, so
    callbacks signed with the pre-import secret no longer verify.
    ``execution_key_fingerprint`` is the bound key's per-mint identity: a turn matches it
    against the live key, so a revoke+remint of the same ``user_id`` fails closed.
    """

    callback_secret: str | None = None
    execution_key_fingerprint: str = Field(
        min_length=1,
        description=(
            "The bound key's per-mint identity the turn resolves against, derived "
            "server-side at create and never client-supplied."
        ),
    )


class ChannelConversationRoute(ChannelConversationRouteCreate):
    """The stored ``channel`` routing row: the create shape plus the bound key's fingerprint (no callback secret)."""

    callback_secret: None = None
    execution_key_fingerprint: str = Field(
        min_length=1,
        description=(
            "The bound key's per-mint identity the turn resolves against, derived "
            "server-side at create and never client-supplied."
        ),
    )


#: The stored routing row the manager persists and backup restore validates, discriminated on ``door``.
ConversationRoute = Annotated[ApiConversationRoute | ChannelConversationRoute, Field(discriminator="door")]


class ConversationRouteBody(RootModel[ConversationRoute]):
    """The validating form of a stored route row: ``.root`` is the door's variant."""
