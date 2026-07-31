"""Conversation bridge contract types.

The messaging bridge turns an inbound message — from an authed API caller or a
registered channel adapter — into an agent turn whose answer is durably stored and
delivered back. This module holds only the wire/record shapes that surface crosses;
the routing-row manager, turn engine, answer store and delivery executor are the
skeleton's, reached through its ``AppConversations`` facet.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Which door a route is reached through: ``api`` delivers by signed callback,
#: ``channel`` delivers back through the medium adapter's ``notify``.
ConversationDoor = Literal["api", "channel"]

#: A turn's answer kind; ``error`` answers carry generic client-safe text only.
AnswerStatus = Literal["answered", "error"]

# Route names key the ``bridge:{route_name}:{address}`` thread namespace, so the
# vocabulary must exclude ``:``.
ROUTE_NAME_RE = re.compile(r"^[a-z0-9-]+$")


class DeliveryReceipt(StrEnum):
    """The terminal fate of an outbound message, as a channel adapter reports it back
    through ``AppConversations.record_delivery_status``. A channel normalizes its
    provider's vocabulary to these two; intermediate states are not modelled.
    """

    DELIVERED = "delivered"
    FAILED = "failed"


def _is_https_url(value: str) -> bool:
    # Must parse (not ``startswith``): rejects hostless ``https://``, scheme-only or
    # relative strings, ``user@host`` authority spoofing, and malformed authorities.
    try:
        split = urlsplit(value)
    except ValueError:
        return False
    return split.scheme == "https" and bool(split.hostname) and "@" not in split.netloc


class ConversationMessage(BaseModel):
    """The client-facing inbound body of the authed API door
    ``POST /api/conversations/{route_name}/messages``.

    ``external_user_id`` is the caller's handle for the end user: it becomes the
    ``client_address`` the answer is delivered against and the conversation's thread
    key. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    external_user_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    wait_seconds: int | None = Field(
        default=None,
        ge=0,
        description="Bounded sync-wait window (seconds); absent = async 202. The door clamps to its runtime cap.",
    )

    @field_validator("external_user_id", "text")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-blank")
        return value


class ConversationAnswer(BaseModel):
    """The answer to one conversation turn — the body POSTed (HMAC-signed) to a
    ``door=api`` row's ``callback_url`` AND the bounded sync-wait payload.

    ``message_id`` correlates it to the ``202``/``200`` the door returned. On
    ``status="error"`` the ``answer`` is generic client-safe text, never an internal
    detail. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    message_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: AnswerStatus
    answer: str = Field(min_length=1)

    @field_validator("answer")
    @classmethod
    def _answer_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer must be non-blank")
        return value


class ConversationRouteCreate(BaseModel):
    """The client-facing create/edit body for a conversation route: the fields a caller
    supplies.

    Binds an ``agent_name`` to an ``execution_key`` the turn runs AS (bound with
    pass-role at create). ``api`` rows carry an https ``callback_url``; ``channel`` rows
    carry the registry ``channel`` plus the ``our_identity`` the medium is texted at (N
    rows may share a channel, each its own identity). The server-derived
    ``callback_secret`` and ``execution_key_fingerprint`` are deliberately absent;
    :class:`ConversationRoute` is this shape plus those. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    # A ``:``-free slug: it keys the ``bridge:{route_name}:{client_address}`` thread
    # namespace, where a ``:`` would let one route's threads collide with another's.
    route_name: str
    door: ConversationDoor
    agent_name: str = Field(min_length=1)
    execution_key: str = Field(
        min_length=1,
        description=(
            "The api-key ``user_id`` the turn runs AS; its live stored grants authorize "
            "the agent run and every tool call the turn makes."
        ),
    )
    channel: str | None = None  # door=channel: the registry name, ``:``-free
    our_identity: str | None = None  # door=channel: the medium address we are texted at
    callback_url: str | None = None  # door=api: the https answer sink

    @field_validator("route_name")
    @classmethod
    def _check_route_name(cls, value: str) -> str:
        if not ROUTE_NAME_RE.fullmatch(value):
            raise ValueError(f"route_name must be a slug matching {ROUTE_NAME_RE.pattern!r}: {value!r}")
        return value

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
            if not (self.callback_url and _is_https_url(self.callback_url)):
                raise ValueError("door=api requires an absolute https callback_url")
            if self.channel is not None:
                raise ValueError("door=api carries no channel")
            if self.our_identity is not None:
                raise ValueError("door=api carries no our_identity")
        return self


class ConversationRoute(ConversationRouteCreate):
    """The stored routing row: :class:`ConversationRouteCreate` plus the two
    server-derived fields. What the manager persists and backup restore validates.

    ``callback_secret`` (``api`` rows) signs the delivery callback; it is excluded from
    export and re-minted per row on import, so callbacks signed with the pre-import
    secret no longer verify. ``execution_key_fingerprint`` is the bound key's per-mint
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


__all__ = [
    "ROUTE_NAME_RE",
    "AnswerStatus",
    "ConversationAnswer",
    "ConversationDoor",
    "ConversationMessage",
    "ConversationRoute",
    "ConversationRouteCreate",
    "DeliveryReceipt",
]
