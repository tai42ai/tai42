"""Multichannel identity: a ``PersonAddress`` endpoint and the ``Person`` it folds into."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from tai42_contract.conversation_target import ConversationTargetKind
from tai42_contract.conversations.routes import ConversationDoor
from tai42_contract.locale import normalize_optional_locale


class PersonAddress(BaseModel):
    """One reachable endpoint of a :class:`Person` on a single target.

    One channel address (or api caller address) the platform folds into that person's identity.

    A ``channel``-door address carries the registry ``channel`` name plus the
    ``our_identity`` the medium is texted at; an ``api``-door address carries ``None`` for
    both — the api caller address is the composed ``caller/end-user`` string, which has no
    channel identity. ``routes`` is EVERY route name this address has written under on the
    target (ordered, deduped): one address can legally reach one target through several
    routes (N api routes; a channel identity re-routed under a new route name), and the
    aggregated person-thread read enumerates the person's routes straight off these rows, so
    a scalar here would silently drop legs. ``address`` is the canonical form the bridge
    already keys threads by. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    door: ConversationDoor
    routes: list[str] = Field(min_length=1)
    channel: str | None = None
    our_identity: str | None = None
    address: str = Field(min_length=1)
    linked_at: datetime

    @field_validator("routes")
    @classmethod
    def _routes_non_blank_and_unique(cls, value: list[str]) -> list[str]:
        if any(not route.strip() for route in value):
            raise ValueError("every route name must be non-blank")
        if len(set(value)) != len(value):
            raise ValueError(f"route names must be unique, got {value!r}")
        return value

    @model_validator(mode="after")
    def _channel_identity_matches_door(self) -> PersonAddress:
        if self.door == "channel":
            if not (self.channel and self.channel.strip()):
                raise ValueError("a channel-door address requires a non-blank channel")
            if not (self.our_identity and self.our_identity.strip()):
                raise ValueError("a channel-door address requires a non-blank our_identity")
        elif self.channel is not None or self.our_identity is not None:
            raise ValueError("an api-door address carries no channel/our_identity")
        return self

    @field_validator("linked_at")
    @classmethod
    def _ensure_tz_aware(cls, value: datetime) -> datetime:
        # The stored form is compared lexically to pick a merge survivor, so it must
        # carry a UTC offset (a naive value sorts before every ``+00:00`` and corrupts
        # the survivor rule); reject a naive datetime and normalize any aware value to
        # UTC (same strictness as InteractionRequest/ChannelDelivery).
        if value.tzinfo is None:
            raise ValueError("linked_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @field_serializer("linked_at")
    def _serialize_linked_at(self, value: datetime) -> str:
        # A single canonical ISO-8601 form for the stored timestamp — see
        # :meth:`Person._serialize_created_at` for why the format is pinned.
        return value.isoformat()


class Person(BaseModel):
    """A single identity on one target.

    The one-or-more :class:`PersonAddress` rows the platform treats as the same person for a
    ``(target_kind, target_name)`` pair.

    A provisional person carries exactly ONE address — its first contact. Explicit pair-code
    redemption merges two persons into one (the union of their addresses); persons never
    cross targets. There is NO greeted flag: the row's existence is itself the first-contact
    marker, so a caller learns a first contact from whether it created the row. Runtime state
    that lives only in the deployment's Redis — deliberately NOT a backup section, the same
    family as a conversation record. Frozen.
    """

    model_config = ConfigDict(frozen=True)

    person_id: str = Field(min_length=1)
    target_kind: ConversationTargetKind
    target_name: str = Field(min_length=1)
    created_at: datetime
    addresses: list[PersonAddress] = Field(min_length=1)
    # The person's BCP 47 locale the rendering layer resolves text against: seeded
    # from the channel at first contact and overridable through the write door, or
    # ``None`` when none is known. Canonical spelling (see
    # :func:`tai42_contract.locale.canonical_locale`); never a silent default.
    locale: str | None = None

    @field_validator("locale")
    @classmethod
    def _canonical_locale(cls, value: str | None) -> str | None:
        return normalize_optional_locale(value)

    @field_validator("created_at")
    @classmethod
    def _ensure_tz_aware(cls, value: datetime) -> datetime:
        # The stored form is compared lexically to pick a merge survivor, so it must
        # carry a UTC offset (a naive value sorts before every ``+00:00`` and corrupts
        # the survivor rule); reject a naive datetime and normalize any aware value to
        # UTC (same strictness as InteractionRequest/ChannelDelivery).
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @field_serializer("created_at")
    def _serialize_created_at(self, value: datetime) -> str:
        # The stored form is compared lexically to pick a merge survivor (earliest first),
        # so every timestamp must serialize to ONE canonical, chronologically-sortable
        # string. ``isoformat`` on a tz-aware UTC value is that form; the default pydantic
        # serializer's trailing ``Z`` would not compare equal to a hand-built ``+00:00``.
        return value.isoformat()
