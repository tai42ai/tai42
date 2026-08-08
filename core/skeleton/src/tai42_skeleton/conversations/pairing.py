"""Pairing: the pure inbound classifier the turn engine dispatches on, and the pair-code
mint behind the ``get_pairing_code`` builtin tool.

:func:`classify` turns one inbound message — with NO model judgement and NO fuzzy matching —
into one of four actions the turn engine dispatches on: mint a fresh pair code (``/link``),
detach the sending address (``/unlink``), redeem a carried pair code, or pass the message
through untouched to the target agent/tool. The two commands match ONLY as the whole trimmed
message (``/link``/``/unlink`` — never ``/link extra``, which is ordinary text); a redemption
matches the first ``LINK-XXXXXXXX`` token anywhere in the raw text, so a code carried inside a
sentence, or pasted from an invite link, still redeems (multiple codes → the first wins). On a
target with multichannel OFF the caller never routes here, so all four forms reach the target
as plain text (byte-identical to today).

:func:`mint_pairing_code` is the tool-side feature body: it resolves the
``(channel, our_identity)`` route exactly as the accept path does (canonicalizing
``our_identity`` first, then the shared
:func:`~tai42_skeleton.conversations.turn._resolve_channel_route` seam), refuses loudly when
the resolved target has multichannel turned off, and otherwise mints a fresh single-use code
for the ``sender`` address — rotating out any code already open for that same conversation. The
RAW code is returned once, here, and never stored recoverably; composing an invite (typed code,
or a channel-web ``?pair=`` URL) around it is the operator's job, never the platform's."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from tai42_contract.conversations import MultichannelDisabledError

from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.pair_codes import ConversationPairCodeStore, MintingConversation
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore

# The D1 pair-code shape (``LINK-`` + 8 ``[A-Z0-9]``), matched as a whole token so a longer
# alphanumeric run adjacent to a valid-looking prefix is not mistaken for a code.
_CODE_RE = re.compile(r"\bLINK-[A-Z0-9]{8}\b")


@dataclass(frozen=True)
class Link:
    """The whole trimmed message was ``/link`` — mint a fresh pair code for this conversation."""


@dataclass(frozen=True)
class Unlink:
    """The whole trimmed message was ``/unlink`` — detach the sending address from its person."""


@dataclass(frozen=True)
class Redeem:
    """The message carries a pair code (the first match) to redeem into a merge."""

    code: str


@dataclass(frozen=True)
class Passthrough:
    """Ordinary text — routed unchanged to the target agent/tool."""


#: An inbound message classifies to exactly one of these four actions.
PairingAction = Link | Unlink | Redeem | Passthrough


def classify(text: str) -> PairingAction:
    """Classify ``text`` into a :data:`PairingAction`. Exact-match commands first (the whole
    trimmed message), then the first embedded pair code, else a passthrough."""
    trimmed = text.strip()
    if trimmed == "/link":
        return Link()
    if trimmed == "/unlink":
        return Unlink()
    match = _CODE_RE.search(text)
    if match is not None:
        return Redeem(match.group(0))
    return Passthrough()


async def mint_pairing_code(channel: str, our_identity: str, sender: str) -> tuple[str, datetime]:
    """Mint a fresh pair code for the ``(channel, our_identity, sender)`` conversation and
    return ``(code, expires_at)``.

    ``channel``/``our_identity`` name the medium address the conversation is texted at (the
    route); ``sender`` is the address the code will link. All three must be non-blank: a
    blank-string argument (``""`` or whitespace) is a loud :class:`ValueError`, since a pair
    code is meaningless without a channel conversation to attribute it to. A null argument
    never reaches here in real dispatch — the ``get_pairing_code`` tool's typed signature
    rejects it by input validation first — though the guard below rejects a bare ``None``
    too, so a direct call is defended just the same.

    Raises:
        ValueError: A blank-string ``channel``, ``our_identity``, or ``sender`` (and,
            defensively on a direct call, a bare ``None``).
        ConversationRouteResolutionError: No channel route matches ``(channel, our_identity)``.
        MultichannelDisabledError: The resolved target has multichannel turned off (D11).
    """
    # Imported inside the call to break a module-level cycle: the turn engine imports
    # ``classify`` from this module, so this module must not import turn at load time.
    from tai42_skeleton.conversations.turn import _resolve_channel_route

    for name, value in (("channel", channel), ("our_identity", our_identity), ("sender", sender)):
        if not value or not value.strip():
            raise ValueError(f"get_pairing_code requires a non-blank {name}")
    identity_canonical = canonical_address(our_identity)
    route = await _resolve_channel_route(channel, identity_canonical)
    settings = ConversationsSettings()
    config = await ConversationTargetConfigStore(settings).get(route.target_kind, route.target_name)
    if config is None or not config.multichannel:
        raise MultichannelDisabledError(
            f"target {route.target_kind}:{route.target_name} has multichannel turned off; no pair code minted"
        )
    conversation = MintingConversation(
        target_kind=route.target_kind,
        target_name=route.target_name,
        route_name=route.route_name,
        door=route.door,
        channel=route.channel,
        our_identity=route.our_identity,
        # The address is stored in the same canonical form the accept path keys a
        # conversation by, so the code redeems onto exactly this sender's thread.
        address=canonical_address(sender),
    )
    return await ConversationPairCodeStore(settings).mint(conversation)


__all__ = ["Link", "PairingAction", "Passthrough", "Redeem", "Unlink", "classify", "mint_pairing_code"]
