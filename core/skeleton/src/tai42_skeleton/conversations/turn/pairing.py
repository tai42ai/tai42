"""The multichannel pairing / redeem turn and its first-contact greeting and code."""

from __future__ import annotations

import logging
import string
from datetime import UTC, datetime

from tai42_contract.conversations import (
    GREETING_PLACEHOLDER,
    ConversationRoute,
    CrossTargetMergeError,
    NotLinkedError,
    PairCodeInvalidError,
    Person,
    PersonAddress,
)

from tai42_skeleton.conversations.pairing import Link, Redeem, Unlink
from tai42_skeleton.conversations.persons import PairingTarget
from tai42_skeleton.conversations.turn import accessors
from tai42_skeleton.conversations.turn.outcome import (
    _pairing_reply,
    _ResolvedOutcome,
    _SilentOutcome,
    _text_part,
    _tool_error,
    _ToolOutcome,
)
from tai42_skeleton.conversations.turn.routing import _Multichannel

logger = logging.getLogger("tai42_skeleton.conversations.turn")

#: A freshly minted pair code and its expiry, as :meth:`ConversationPairCodeStore.mint` returns.
_MintedCode = tuple[str, datetime]

# Fixed, generic pairing-turn replies — no channel names, no links (operators who want
# richer wording compose it from the pairing tool + their own flow).
_LINKED_TEXT = "Done — this conversation is now linked to your other one."
_UNLINKED_TEXT = "Done — this conversation is no longer linked."
_NOT_LINKED_TEXT = "This conversation is not linked to anything, so there is nothing to unlink."
# The UNIFORM redeem refusal: an unknown/expired/already-redeemed code, a cross-target code,
# or a throttled attempt all read the same, so the reply reveals no oracle.
_INVALID_CODE_TEXT = "That pairing code is not valid. It may have expired or already been used."


def _template_references_code(template: str) -> bool:
    """Whether a validated greeting template references ``{pairing_code}`` (vs a fixed string).

    A code is minted ONLY when the greeting will actually carry one.
    """
    return any(field == GREETING_PLACEHOLDER for _literal, field, _spec, _conv in string.Formatter().parse(template))


async def _greeting_and_code(multichannel: _Multichannel) -> tuple[str | None, _MintedCode | None]:
    """The rendered first-contact greeting for a created-now person and the code it minted.

    Returns ``(None, None)`` when the target configures no template. ``{pairing_code}``
    is substituted with a freshly minted code (rotating any open one) that is RETURNED
    so a same-turn ``/link`` can present that SAME live code instead of minting a
    second one; a template with no placeholder mints nothing and returns no code.
    """
    template = multichannel.greeting_template
    if template is None:
        return None, None
    if _template_references_code(template):
        minted = await accessors._pair_code_store().mint(multichannel.minting_conversation())
        return template.format(pairing_code=minted[0]), minted
    return template.format(), None


def _with_greeting(outcome: _ToolOutcome, greeting: str | None) -> _ToolOutcome:
    r"""Prepend a due greeting as its own LEADING message.

    A greeting is a message of its own, so it becomes the first ordered part ahead of
    the turn's parts; a silent outcome due a greeting becomes an answered greeting-only
    reply — a greeting, once due, is never silently dropped; an error outcome keeps the
    greeting ahead of its client-safe text. The joined answer for a single-part outcome
    renders as ``f"{greeting}\n\n{answer}"``.
    """
    if greeting is None:
        return outcome
    if isinstance(outcome, _SilentOutcome):
        return _ResolvedOutcome(answer_status="answered", parts=[_text_part(greeting)], error=None)
    return _ResolvedOutcome(
        answer_status=outcome.answer_status, parts=[_text_part(greeting), *outcome.parts], error=outcome.error
    )


async def _run_pairing_turn(
    multichannel: _Multichannel,
    person: Person,
    action: Link | Unlink | Redeem,
    greeting_code: _MintedCode | None,
    route: ConversationRoute,
) -> _ToolOutcome:
    """Dispatch a classified pairing action against the multichannel target and return its outcome.

    ``Link`` presents ``greeting_code`` when a first-contact greeting already minted
    one this turn (so only ONE code is minted and the greeting's code stays live), else
    mints its own; ``Redeem`` redeems then ensures BOTH sides and merges; ``Unlink``
    detaches the sending address.

    Error scoping: ONLY the named pairing domain errors (``PairCodeInvalidError``,
    ``CrossTargetMergeError``, ``NotLinkedError``) become the uniform answered refusal — it
    IS the answer, detail logged. ANY other exception (a redis fault, a reset) takes
    the platform's standard client-safe ``error`` outcome, so an infra fault never
    masquerades as an invalid code.
    """
    try:
        if isinstance(action, Link):
            code, expires_at = greeting_code or await accessors._pair_code_store().mint(
                multichannel.minting_conversation()
            )
            return _pairing_reply(_link_reply(code, expires_at))
        if isinstance(action, Unlink):
            await accessors._person_store().detach(
                person.person_id,
                door=multichannel.door,
                channel=multichannel.channel,
                our_identity=multichannel.our_identity,
                address=multichannel.address,
            )
            return _pairing_reply(_UNLINKED_TEXT)
        return await _redeem_turn(multichannel, person, action)
    except NotLinkedError as exc:
        logger.info("conversations: /unlink refused on route %r: %s", multichannel.route_name, exc)
        return _pairing_reply(_NOT_LINKED_TEXT)
    except (PairCodeInvalidError, CrossTargetMergeError) as exc:
        logger.info("conversations: pairing refused on route %r: %s", multichannel.route_name, exc)
        return _pairing_reply(_INVALID_CODE_TEXT)
    except Exception as exc:
        logger.exception("conversations: pairing turn on route %r failed", multichannel.route_name, exc_info=exc)
        return _tool_error(f"pairing turn error: {exc}", route)


async def _redeem_turn(multichannel: _Multichannel, person: Person, action: Redeem) -> _ToolOutcome:
    """Redeem a pair code and merge the two persons — behind the brute-force throttle.

    A locked source, and an invalid code, both return the SAME uniform reply (no
    oracle); a valid redeem clears the throttle. The minting side's provisional row is
    ensured from the code's stored value (a tool-minted code may name an address with
    no admitted inbound yet); such a row consumes no greeting — the person is already
    linked, and the greeting predicate fires only at that address's OWN admitted
    inbound, which this is not.
    """
    throttle = accessors._redeem_throttle()
    source = multichannel.throttle_source_key()
    if await throttle.is_locked(multichannel.target, source):
        return _pairing_reply(_INVALID_CODE_TEXT)
    try:
        minting = await accessors._pair_code_store().redeem(action.code)
    except PairCodeInvalidError:
        await throttle.record_failure(multichannel.target, source)
        return _pairing_reply(_INVALID_CODE_TEXT)
    await throttle.clear(multichannel.target, source)
    minting_person, _created = await accessors._person_store().ensure_provisional(
        PairingTarget(target_kind=minting.target_kind, target_name=minting.target_name),
        PersonAddress(
            door=minting.door,
            routes=[minting.route_name],
            channel=minting.channel,
            our_identity=minting.our_identity,
            address=minting.address,
            linked_at=datetime.now(UTC),
        ),
    )
    await accessors._person_store().merge(person.person_id, minting_person.person_id)
    return _pairing_reply(_LINKED_TEXT)


def _link_reply(code: str, expires_at: datetime) -> str:
    """The fixed neutral ``/link`` reply carrying the fresh code and its expiry."""
    return (
        f"Your pairing code is {code}. Send it from your other conversation to link the two. "
        f"It expires at {expires_at.isoformat()}."
    )
