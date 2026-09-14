"""The inbound descriptor, subject/locale, ambient state context and run attribution a turn
surfaces on its payload and deposits around its target run.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.conversations import ConversationRoute, Person
from tai42_contract.monitoring import RunAttribution
from tai42_contract.states import StateContext, SubjectCandidates

from tai42_skeleton.conversations.models import ConversationRecord


def _inbound_id_and_source(record: ConversationRecord, route: ConversationRoute) -> tuple[str | None, str | None]:
    """The inbound descriptor's ``id`` and ``source``: the channel provider id / the event
    id / the api record id, and the channel name / ``event:{kind}`` / ``api`` — the door the
    turn entered through, named generically. Shared by the ``turn`` block and the ambient
    conversation state context so both name the same inbound message."""
    if record.inbound_kind == "event":
        # The model invariant guarantees an event record carries its ``inbound_event``.
        event = record.inbound_event or {}
        return event["event_id"], f"event:{event['kind']}"
    if record.door == "channel":
        return record.provider_message_id, route.channel
    return record.message_id, "api"


def _resolved_locale(person: Person | None, record: ConversationRecord, route: ConversationRoute) -> str | None:
    """The subject's locale for the rendering layer, by precedence: a person's STORED locale
    (an operator override or a first-contact seed) wins, else the channel's per-message hint
    on this record, else the route's operator-declared default, else ``None`` — the explicit
    "no locale known" the renderer never silently defaults away. Every stored form is already
    canonical, so no reparse here."""
    if person is not None and person.locale is not None:
        return person.locale
    if record.inbound_locale is not None:
        return record.inbound_locale
    return route.locale


def _turn_block(
    record: ConversationRecord, route: ConversationRoute, *, person: Person | None, thread_id: str
) -> dict[str, object]:
    """The generic ``turn`` block surfaced on EVERY tool turn's payload: the turn id (the
    record's ``message_id``, no new mint), the inbound descriptor (``inbound.id`` /
    ``inbound.kind`` message/event / ``inbound.source``), and the ``subject`` block a flow's
    ``subject_expr`` reads — the target scope plus the ``person`` id (null off a
    non-multichannel target) and the resolved ``thread`` id, the same candidates the ambient
    state context carries."""
    inbound_id, source = _inbound_id_and_source(record, route)
    return {
        "id": record.message_id,
        "inbound": {"id": inbound_id, "kind": record.inbound_kind, "source": source},
        "subject": {
            "target_kind": route.target_kind,
            "target_name": route.target_name,
            "person": person.person_id if person is not None else None,
            "thread": thread_id,
            "locale": _resolved_locale(person, record, route),
        },
    }


def _conversation_state_context(
    route: ConversationRoute, intake: ConversationRecord, person: Person | None, *, actor: str | None
) -> StateContext:
    """The ambient :class:`StateContext` a conversation turn deposits so every downstream
    state write resolves its subject and completes its provenance from this one door.

    The candidates are the ``thread`` (always) and the ``person`` id (only a multichannel
    target resolves a person), keyed under the route's target scope; ``actor`` is the turn's
    generic attribution ``user_id``, ``turn_id`` the intake's ``message_id`` and ``inbound_id``
    the inbound message the turn answers — the same identity the run trace is stamped with."""
    by_kind: dict[str, str] = {"thread": intake.thread_id}
    if person is not None:
        by_kind["person"] = person.person_id
    inbound_id, _source = _inbound_id_and_source(intake, route)
    return StateContext(
        door="conversation",
        candidates=SubjectCandidates(
            target_kind=route.target_kind,
            target_name=route.target_name,
            by_kind=by_kind,
            locale=_resolved_locale(person, intake, route),
        ),
        actor=actor,
        turn_id=intake.message_id,
        inbound_id=inbound_id,
    )


def _conversation_attribution(
    route: ConversationRoute, intake: ConversationRecord, person: Person | None
) -> RunAttribution:
    """The generic :class:`RunAttribution` a conversation turn's run trace is stamped with.

    ``user_id`` is person-FIRST — a resolved (linked or provisional) person's stable
    ``person_id`` — falling back to the raw ``{channel}:{client_address}`` the door saw
    when no person exists (the plain, non-multichannel path). ``session_id`` is the
    RESOLVED thread — the route-keyed ``bridge:{route}:{address}`` or the ``@person``
    aggregated thread — so a person's runs across channels group under one session.
    ``tags`` carry the route; ``metadata`` carries the channel + our-identity (present
    only for a channel door). All generic — the platform assigns no meaning."""
    metadata: dict[str, Any] = {}
    if intake.channel is not None:
        metadata["channel"] = intake.channel
    if intake.our_identity is not None:
        metadata["our_identity"] = intake.our_identity
    if person is not None:
        user_id = person.person_id
    elif intake.channel is not None:
        user_id = f"{intake.channel}:{intake.client_address}"
    else:
        # API door with no resolved person: the address alone — never a literal
        # "None:" prefix, since this string doubles as the erasure key.
        user_id = intake.client_address
    return RunAttribution(
        user_id=user_id,
        session_id=intake.thread_id,
        tags=[f"route:{route.route_name}"],
        metadata=metadata,
    )
