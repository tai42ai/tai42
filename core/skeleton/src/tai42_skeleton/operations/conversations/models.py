"""Request/query/body DTOs and paging constants for the conversation-route doors.

Plus the option/section type adapters the send seam coerces raw dicts through.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
from tai42_contract.channels import Option, OptionSection
from tai42_contract.conversations import ConversationMode

# Coerces raw option dicts into the typed ``Option`` discriminated union once, so the send seam
# and the AnswerPart validation both see typed models rather than raw dicts.
_OPTIONS_ADAPTER = TypeAdapter(list[Option])
# The sectioned-options counterpart, coerced once so the send seam and the AnswerPart validation
# both see typed ``OptionSection`` models rather than raw dicts.
_SECTIONS_ADAPTER = TypeAdapter(list[OptionSection])

#: The largest page either thread read door serves. A larger ``page_size`` is capped to it —
#: valid data, not an error — so one read can never ask for an unbounded slice.
MAX_THREAD_PAGE_SIZE = 200

#: The highest page number either thread read door serves. A page past it names a rank the
#: index cannot be sliced at, which the backend answers with an error of its own; it is a
#: malformed window and is refused as one here.
MAX_THREAD_PAGE = 1_000_000

#: The transcript orders the door serves: ``asc`` is the transcript order (oldest first),
#: ``desc`` the live-tail order, where page 1 always holds the newest messages.
TranscriptOrder = Literal["asc", "desc"]
TRANSCRIPT_ORDERS = get_args(TranscriptOrder)


class ThreadWindowQuery(BaseModel):
    """The ``?page=``/``?pageSize=`` window the route thread listing takes.

    Spec metadata only — the door parses its query at the HTTP edge.
    """

    page: int = Field(default=1, ge=1, le=MAX_THREAD_PAGE, description="1-based page number, newest activity first.")
    page_size: int = Field(
        default=50,
        ge=1,
        alias="pageSize",
        description=f"Items per page. A larger value is capped to {MAX_THREAD_PAGE_SIZE}, never refused.",
    )


class ThreadListQuery(ThreadWindowQuery):
    """The thread listing's optional filters on top of the shared window.

    Both are a BOUNDED app-side post-scan (no per-route+status index), so a filtered page may
    report ``truncated``.

    Spec metadata only — the door parses its query at the HTTP edge.
    """

    status: str | None = Field(
        default=None,
        description="Keep only threads whose newest record's delivery_status is this one "
        "(validated against the delivery-status vocabulary; an unknown value is a 400).",
    )
    address: str | None = Field(
        default=None,
        description="Keep only threads whose id client-address suffix contains this substring.",
    )


class TranscriptQuery(ThreadWindowQuery):
    """The transcript door's query on top of the shared window.

    ``thread_id`` is REQUIRED and holds at least one non-whitespace character — a client generated
    without it calls the door with no thread to read, and a blank one names no thread; both are
    answered 400.
    """

    page: int = Field(
        default=1, ge=1, le=MAX_THREAD_PAGE, description="1-based page number, in the requested ``order``."
    )
    thread_id: str = Field(min_length=1, pattern=r"\S", description="The thread to read, as the send door returned it.")
    order: TranscriptOrder = Field(
        default="asc",
        description="``asc`` reads the transcript oldest first; ``desc`` is the live-tail order.",
    )
    q: str | None = Field(
        default=None,
        description="Optional text filter — keep only records whose inbound text or answer "
        "contains this substring (a BOUNDED scan, so a filtered page may report ``truncated``).",
    )


class MessageSearchQuery(ThreadWindowQuery):
    """The route-scoped message-search door's query.

    ``q`` is REQUIRED and holds at least one non-whitespace character; the search is a BOUNDED scan
    across the route's threads, so a page may report ``truncated``.

    Spec metadata only — the door parses its query at the HTTP edge.
    """

    q: str = Field(min_length=1, pattern=r"\S", description="The text to search the route's records for.")


class ThreadDeleteQuery(BaseModel):
    """The thread-delete door's ``?thread_id=`` query.

    ``thread_id`` is REQUIRED and holds at least one non-whitespace character — a client generated
    without it calls the door with no thread to forget, and a blank one names no thread; both are
    answered 400.

    Spec metadata only — the door parses its query at the HTTP edge.
    """

    thread_id: str = Field(
        min_length=1, pattern=r"\S", description="The thread to forget, as the send door returned it."
    )


class ThreadMessageSend(BaseModel):
    """The operator-send door's JSON body.

    ``thread_id`` and ``text`` are required and hold at least one non-whitespace character; the
    remaining fields are optional richer-send forms delivered alongside ``text``. The ``schema``
    attribute is suffixed to avoid shadowing a ``BaseModel`` member; the wire key stays ``schema``
    via the alias.

    Spec metadata only — the door parses this body at the HTTP edge.
    """

    model_config = ConfigDict(populate_by_name=True)

    thread_id: str = Field(
        min_length=1, pattern=r"\S", description="The thread to send into, as the send door returned it."
    )
    text: str = Field(min_length=1, pattern=r"\S", description="The message text to send.")
    address: str | None = Field(
        default=None,
        description="On a linked person's aggregated thread, the person address to target; "
        "omitted, the target is the thread's newest record.",
    )
    media: list[dict[str, JsonValue]] | None = Field(
        default=None, description="Display media items delivered alongside the text."
    )
    template: dict[str, JsonValue] | None = Field(
        default=None, description="A pre-approved out-of-window template to deliver."
    )
    options: list[dict[str, JsonValue]] | None = Field(
        default=None, description="Flat tappable option objects — reply or link actions."
    )
    schema_: dict[str, JsonValue] | None = Field(
        default=None,
        alias="schema",
        description="An ask-less form's answer schema; the channel renders ``text`` as the prompt.",
    )
    location: dict[str, JsonValue] | None = Field(
        default=None, description="A shared map pin ``{latitude, longitude, name?, address?}``."
    )
    sections: list[dict[str, JsonValue]] | None = Field(
        default=None, description="Titled groups of tappable reply rows — the sectioned options alternative."
    )
    header: dict[str, JsonValue] | None = Field(
        default=None, description="A media header composing an interactive message."
    )
    footer: str | None = Field(default=None, description="A trailing line composing an interactive message.")


class ThreadModeSet(BaseModel):
    """The mode-set door's JSON body ``{thread_id, mode}``.

    ``thread_id`` holds at least one non-whitespace character and ``mode`` is one of the
    control-mode vocabulary.

    Spec metadata only — the door parses this body at the HTTP edge.
    """

    thread_id: str = Field(
        min_length=1, pattern=r"\S", description="The thread whose mode override to set, as the send door returned it."
    )
    mode: ConversationMode = Field(description="The control mode to set: ``agent`` or ``manual``.")
