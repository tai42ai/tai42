"""A web route's own words: the chat page's refusal pages and the widget's refusal sentences.

``WebWords`` is the per-identity object the words doors store and the page door reads. Every
member is optional, but a stored object sets at least one sentence, and a member that is given
sets something; an unset member draws the built-in English beside it here (``page``) or in the
widget's own table (``transport``). A set member is a non-blank participant-facing text
(:data:`~tai42_contract.interactions.ReplyText`). The ``transport`` members carry the tokens their
descriptions name, filled by the widget with a plain split/join.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from tai42_contract.interactions import ReplyText

#: The built-in sentence of a gated route's refusal: the entry-refused page's and the rotate door's.
ENTRY_REFUSED_MESSAGE: Final = "This chat is private. Open it from the link you were given."


class _Words(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RefusalPageWords(_Words):
    """One refusal page: its heading (the page title and ``<h1>``) and its sentence."""

    heading: ReplyText | None = None
    text: ReplyText | None = None

    def is_empty(self) -> bool:
        """``True`` when neither the heading nor the sentence is set."""
        return self.heading is None and self.text is None


class PageWords(_Words):
    """The chat page door's refusal pages, one per refusal a visitor's navigation can meet."""

    not_a_navigation: RefusalPageWords = Field(
        default_factory=RefusalPageWords,
        description="The chat URL was loaded by something other than a top-level navigation (an embed or a script).",
    )
    link_invalid: RefusalPageWords = Field(
        default_factory=RefusalPageWords,
        description="The chat link's query parameters break a bound (a repeated name, a bad name, too long, too many).",
    )
    entry_refused: RefusalPageWords = Field(
        default_factory=RefusalPageWords,
        description="The route is gated and the link carries no live entry code.",
    )
    page_unavailable: RefusalPageWords = Field(
        default_factory=RefusalPageWords,
        description="The chat page bundle on the server cannot be served.",
    )

    @field_validator("not_a_navigation", "link_invalid", "entry_refused", "page_unavailable")
    @classmethod
    def _given_page_sets_something(cls, page: RefusalPageWords) -> RefusalPageWords:
        if page.is_empty():
            raise ValueError("a refusal page that is given sets its heading, its text or both")
        return page

    def is_empty(self) -> bool:
        """``True`` when no refusal page sets anything."""
        return all(getattr(self, name).is_empty() for name in type(self).model_fields)


class TransportWords(_Words):
    """The sentences the widget draws when one of its own doors refuses a request."""

    session_ended: ReplyText | None = Field(default=None, description="The visitor's session is gone (HTTP 401).")
    wrong_site: ReplyText | None = Field(default=None, description="The request came from another site (HTTP 403).")
    not_listening: ReplyText | None = Field(default=None, description="No route answers this chat (HTTP 404).")
    already_answered: ReplyText | None = Field(default=None, description="The question was already answered (409).")
    busy: ReplyText | None = Field(default=None, description="The assistant is shedding load (HTTP 429 or 503).")
    not_switched_on: ReplyText | None = Field(default=None, description="The deployment runs no chat store (501).")
    too_long: ReplyText | None = Field(
        default=None, description="The request is over the size cap (HTTP 413). Token: {subject}."
    )
    not_sent: ReplyText | None = Field(
        default=None, description="The door refused the request's content (HTTP 400 or 422). Token: {subject}."
    )
    failed: ReplyText | None = Field(default=None, description="Any other failure of the door.")
    attachment_gone: ReplyText | None = Field(
        default=None, description="An attachment can no longer be sent with the message."
    )
    attachment_already_sent: ReplyText | None = Field(
        default=None, description="An attachment was already sent with another message."
    )
    entry_refused: ReplyText | None = Field(
        default=None, description="A new conversation was refused by the route's entry gate."
    )
    answer_refused: ReplyText | None = Field(
        default=None,
        description="An answer was refused and may be given again, when the send set no sentence. Token: {reason}.",
    )
    answer_retries_spent: ReplyText | None = Field(
        default=None,
        description="An answer was refused and the question can no longer be answered, when the send set no "
        "sentence. Token: {reason}.",
    )
    subject_message: ReplyText | None = Field(default=None, description="The {subject} of a chat message.")
    subject_answer: ReplyText | None = Field(default=None, description="The {subject} of a question's answer.")
    subject_form: ReplyText | None = Field(default=None, description="The {subject} of a form.")
    subject_request: ReplyText | None = Field(
        default=None, description="The {subject} of any other request (a new conversation, the chat stream)."
    )


class WebWords(_Words):
    """A web route's words: its refusal pages (``page``) and the widget's refusal sentences (``transport``)."""

    page: PageWords = Field(default_factory=PageWords)
    transport: TransportWords = Field(default_factory=TransportWords)

    @field_validator("page")
    @classmethod
    def _given_pages_set_something(cls, page: PageWords) -> PageWords:
        if page.is_empty():
            raise ValueError("the page words that are given set at least one refusal page")
        return page

    @field_validator("transport")
    @classmethod
    def _given_transport_sets_something(cls, transport: TransportWords) -> TransportWords:
        if not transport.model_dump(exclude_none=True):
            raise ValueError("the transport words that are given set at least one sentence")
        return transport

    @model_validator(mode="after")
    def _sets_something(self) -> WebWords:
        if self.page.is_empty() and not self.transport_payload():
            raise ValueError("the words set at least one sentence; clear them to draw the built-in English")
        return self

    def transport_payload(self) -> dict[str, str]:
        """The set ``transport`` members, the map the chat page hands the widget (empty when none is set)."""
        return self.transport.model_dump(exclude_none=True)


#: The built-in refusal pages, drawn for every member a route leaves unset.
DEFAULT_PAGE_WORDS: Final[dict[str, tuple[str, str]]] = {
    "not_a_navigation": (
        "Open the chat in its own page",
        "This chat starts a session only when its page is opened directly. "
        "Open the chat link in your browser to start chatting.",
    ),
    "link_invalid": (
        "This chat link is not valid",
        "This chat link is not valid. Please check the link and try again.",
    ),
    "entry_refused": ("This chat is private", ENTRY_REFUSED_MESSAGE),
    "page_unavailable": ("Chat is unavailable", "The chat page could not be loaded. Please try again later."),
}


def page_refusal(words: WebWords | None, member: str) -> tuple[str, str]:
    """The heading and sentence of one refusal page: the route's own where set, else the built-in."""
    heading, text = DEFAULT_PAGE_WORDS[member]
    if words is None:
        return heading, text
    own: RefusalPageWords = getattr(words.page, member)
    return own.heading or heading, own.text or text


def stored_payload(words: WebWords) -> dict[str, object]:
    """The words as stored, exported and answered: every set member, nothing unset."""
    payload = words.model_dump(exclude_none=True)
    page = {name: entry for name, entry in payload.get("page", {}).items() if entry}
    out: dict[str, object] = {}
    if page:
        out["page"] = page
    if payload.get("transport"):
        out["transport"] = payload["transport"]
    return out
