"""The ``get_pairing_code`` builtin: mint a single-use pair code for a live channel
conversation so the person can prove, from another channel, that both addresses are theirs.

A thin, LLM-facing shim over
:func:`tai42_skeleton.conversations.pairing.mint_pairing_code`. It returns ONLY
``{"code", "expires_at"}`` — no link and no wording; the operator composes the invite text
(a typed code, or a channel-web ``?pair=`` URL) around it.

Authorization fence: a plain builtin takes the capability path — no per-call scope check —
so its reach is bounded by manifest exposure (a deployment opts the module in through a
``tools[].module`` row) plus the tool's own refusals: it raises on a target with
multichannel turned off, while the api door — carrying no channel identity to mint a pair
code against — is turned away earlier still, by input validation of the typed signature.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.conversations.pairing import mint_pairing_code


@tai42_app.tools.tool(tags={"conversations"})
async def get_pairing_code(channel: str, our_identity: str, sender: str) -> dict[str, str]:
    """Mint a fresh single-use pair code for a channel conversation.

    Rotates out any code already open for the same conversation (the newest code wins), so
    calling this again simply issues a new code and invalidates the previous one.

    Args:
        channel: The registry channel name the conversation runs on (e.g. ``telegram``).
        our_identity: The medium address the conversation is texted at — the route's
            identity for ``channel`` (a bot id, phone-number id, ...).
        sender: The address the code will link — the party the code is minted for.

    Returns:
        ``{"code": <the raw pair code>, "expires_at": <ISO-8601 UTC expiry>}`` and nothing
        else. The raw code is returned once here and never stored recoverably.

    Raises:
        ValueError: A blank-string ``channel``, ``our_identity``, or ``sender`` (``""`` or
            whitespace). A null or absent argument — as the api door carries — is refused
            earlier by input validation (the typed signature), before this body runs.
        ConversationRouteResolutionError: No channel route matches ``(channel, our_identity)``.
        MultichannelDisabledError: The resolved target has multichannel turned off.
    """
    code, expires_at = await mint_pairing_code(channel, our_identity, sender)
    return {"code": code, "expires_at": expires_at.isoformat()}
