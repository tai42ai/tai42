"""When an open form reacts, and the events it fires.

``FormReactions`` declares the triggers of a reacting form; ``FormReactionEventKind`` and the
``FieldChangedEvent``/``PageAdvancedEvent``/``SubmittedEvent`` models are the events those
triggers fire, discriminated on ``kind`` as ``FormReactionEvent``; ``FormReactionEventBody`` is
the published form of one event.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel


class FormReactions(BaseModel):
    """When an open form reacts — plain, renderer-readable trigger description. Frozen.

    A reacting form names a handler on the request (``InteractionRequest.reaction_tool``);
    this declares the three events that fire it, each optional and absent-by-default (an
    absent ``reactions`` block is a static form, today's behavior). ``field_changed``
    names the input fields whose change triggers a reaction; ``page_advanced`` names the
    pages whose advance triggers one; ``submitted`` is whether the submission is checked by
    a reaction before it is accepted — the server runs that check on every answer door before
    it records, so a client that ran it first only shows errors early and can never bypass it.
    ``choices`` names the fields whose CHOICE LIST a
    reaction may supply or replace while the form is open (the slots for a date just
    picked, say): for such a field the platform's static submit check validates its TYPE
    only — never membership in the send-time list, which the reaction replaced — so the
    consumer owns membership at the ``submitted`` event. A form declaring any ``choices``
    field therefore MUST also set ``submitted`` (the request model raises otherwise), so an
    unvetted reaction-fed value always meets a consumer submit check. Unknown event kinds
    are refused (``extra='forbid'``); the named fields/pages/choices are cross-checked
    against the schema/pages by
    :func:`~tai42_contract.interactions.models.forms.check_form_reactions`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_changed: list[str] = []
    page_advanced: list[str] = []
    submitted: bool = False
    choices: list[str] = []

    def has_trigger(self) -> bool:
        """Whether this declares at least one reaction trigger (a field change, a page advance, or submit)."""
        return bool(self.field_changed or self.page_advanced or self.submitted)


class FormReactionEventKind(StrEnum):
    """The event kinds a reacting form fires on; one per :class:`FormReactions` trigger."""

    FIELD_CHANGED = "field_changed"
    PAGE_ADVANCED = "page_advanced"
    SUBMITTED = "submitted"


class FieldChangedEvent(BaseModel):
    """A declared ``field_changed`` input field changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[FormReactionEventKind.FIELD_CHANGED] = FormReactionEventKind.FIELD_CHANGED
    field: str = Field(min_length=1)


class PageAdvancedEvent(BaseModel):
    """A declared ``page_advanced`` page was advanced past."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[FormReactionEventKind.PAGE_ADVANCED] = FormReactionEventKind.PAGE_ADVANCED
    page: str = Field(min_length=1)


class SubmittedEvent(BaseModel):
    """The form was submitted and its ``submitted`` reaction checks it before it is accepted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[FormReactionEventKind.SUBMITTED] = FormReactionEventKind.SUBMITTED


#: One form-reaction event, discriminated on ``kind``.
FormReactionEvent = Annotated[FieldChangedEvent | PageAdvancedEvent | SubmittedEvent, Field(discriminator="kind")]


class FormReactionEventBody(RootModel[FormReactionEvent]):
    """The published form of one form-reaction event."""
