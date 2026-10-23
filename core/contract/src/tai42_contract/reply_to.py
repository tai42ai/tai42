"""The quoted-message model: what an inbound reply's quoted message resolved to.

A top-level leaf, re-exported from :mod:`tai42_contract.conversations`. It cannot live under the
``conversations`` package: a submodule import runs that package's ``__init__``, which imports the
interactions package, and the interactions models (:class:`~tai42_contract.interactions.AskAnswer`)
carry this model, so routing it through the package would close an import cycle. This leaf
imports only ``enum`` and pydantic.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class ReplyToStatus(StrEnum):
    """What the platform found for the message an inbound reply quotes."""

    #: The thread's record holds the quoted message; ``text`` is its text.
    FOUND = "found"
    #: This thread's record holds no message with this id: it may be held in an earlier thread of
    #: the same end user, or it expired, was forgotten or was never recorded.
    NOT_FOUND = "not_found"
    #: The quoted message is an answer to a sensitive question; its text is never kept.
    SENSITIVE = "sensitive"
    #: The record holds the message and it has no text: a media-only message, or, on the API door,
    #: a record under which no text was delivered (no outcome yet, an answerless outcome such as
    #: silent, merged or superseded, or an answer of media only).
    NO_TEXT = "no_text"


class ReplyTo(BaseModel):
    """The message an inbound reply quotes, as the platform resolved it from the thread's record.

    ``message_id`` is the id the end user's side names the quoted message by: the medium's message
    id on a channel, the platform's record ``message_id`` on the API door. ``status`` says what the
    platform found (:class:`ReplyToStatus`). ``text`` is the quoted message's text when ``status``
    is ``found``, and ``None`` for every other status. Frozen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str
    status: ReplyToStatus
    text: str | None

    @field_validator("message_id")
    @classmethod
    def _non_blank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message_id must be non-blank")
        return value

    @model_validator(mode="after")
    def _text_matches_status(self) -> ReplyTo:
        if self.status is ReplyToStatus.FOUND:
            if self.text is None or not self.text.strip():
                raise ValueError("text must be non-blank when status is 'found'")
        elif self.text is not None:
            raise ValueError(f"text must be None when status is {self.status.value!r}")
        return self


__all__ = ["ReplyTo", "ReplyToStatus"]
