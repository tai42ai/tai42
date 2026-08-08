"""The single-use pair-code store — mint a code in one conversation, redeem it in another to
fold the two into one person on the same target.

Same single-use posture as the one-time claim-link store: a code is ``sha256``-at-rest, minted
with ``SET ... EX NX`` (retry once on the astronomically rare collision, then raise) and redeemed
with an atomic ``GETDEL`` so exactly one caller can ever win. The RAW code is never a key nor a
value anywhere — it exists only in flight, in the reply that carries it.

Rotation (one active code per minting conversation): an open-pointer key
(``conversations:open_code:<opaque-hash>``) holds the sha256 of the currently open code. A fresh
mint, in ONE Lua step, deletes the code the pointer names, writes the new sha-keyed record with a
TTL, and repoints the pointer — so minting again ROTATES: the previous code stops working the
moment a new one is issued. Re-mint is never idempotent (returning the same raw code would require
storing it recoverably, which is forbidden).

Redeem is a bare ``GETDEL`` of the sha-keyed record and deliberately does NOT touch the open
pointer: only mint reads the pointer, a stale pointer merely makes the next mint delete an
already-gone code key (a no-op), and it dies by TTL or the next mint's overwrite. Every miss —
unknown, expired, or already redeemed — raises the UNIFORM :class:`PairCodeInvalidError`, so the
surface leaks no oracle.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from tai42_contract.conversations import (
    ConversationDoor,
    ConversationTargetKind,
    PairCodeInvalidError,
)
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton.conversations.persons import _as_str, _door_address_key
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.utils.redis_typing import awaited, eval_script

_NO_BACKEND = "conversation pair codes require the redis conversations backend"

_INVALID_CODE = "unknown, expired, or already redeemed pair code"

# The pair-code alphabet and shape (``LINK-`` followed by 8 ``[A-Z0-9]`` characters).
# Generated with the ``secrets`` CSPRNG.
_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_CODE_BODY_LENGTH = 8

# One atomic mint-with-rotation. KEYS[1]=open-code pointer, KEYS[2]=new code record key.
# ARGV = new_code_hash, record_json, ttl_seconds, pair_code_key_prefix. A pre-existing new
# record key (an entropy collision) writes NOTHING and signals 0 so the caller retries; else
# the previously open code (named by the pointer's stored hash) is deleted, the new record and
# the pointer are written with the same TTL, and 1 is returned.
_MINT_LUA = """
-- conversations:pair_code:mint
local open_key, new_code_key = KEYS[1], KEYS[2]
local new_hash, record_json, ttl, code_prefix = ARGV[1], ARGV[2], tonumber(ARGV[3]), ARGV[4]
if redis.call('EXISTS', new_code_key) == 1 then
  return 0
end
local open_hash = redis.call('GET', open_key)
if open_hash then
  redis.call('DEL', code_prefix .. open_hash)
end
redis.call('SET', new_code_key, record_json, 'EX', ttl)
redis.call('SET', open_key, new_hash, 'EX', ttl)
return 1
"""


@dataclass(frozen=True)
class MintingConversation:
    """The conversation a pair code was minted in — everything the redeem side needs to write a
    complete :class:`~tai42_contract.conversations.PersonAddress` for it and know its target.
    ``route_name`` is included because every mint site has just resolved its route, and the
    redeem side would otherwise have no route to attribute the minting address to."""

    target_kind: ConversationTargetKind
    target_name: str
    route_name: str
    door: ConversationDoor
    channel: str | None
    our_identity: str | None
    address: str


def _generate_code() -> str:
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_BODY_LENGTH))
    return f"LINK-{body}"


class ConversationPairCodeStore:
    """The Redis-backed single-use pair-code store. Construction refuses with a loud 501 without
    the redis conversations backend — a minted code must outlive the minting request in durable
    state, never per-worker memory."""

    def __init__(self, settings: ConversationsSettings) -> None:
        if settings.in_memory:
            raise NotSupportedError(_NO_BACKEND)
        self.settings = settings

    async def mint(self, conversation: MintingConversation) -> tuple[str, datetime]:
        """Mint a FRESH code for ``conversation`` (rotating out any code already open for it)
        and return ``(code, expires_at)``. The raw code is returned once here and never stored;
        only its sha256 keys the record."""
        ttl = self.settings.pair_code_ttl_seconds
        dak = _door_address_key(
            door=conversation.door,
            channel=conversation.channel,
            our_identity=conversation.our_identity,
            address=conversation.address,
        )
        open_key = self.settings.open_code_key(conversation.target_kind, conversation.target_name, dak)
        record_json = json.dumps(asdict(conversation), separators=(",", ":"))
        async with client_ctx(RedisClient, self.settings.redis) as r:
            for _attempt in range(2):
                code = _generate_code()
                code_hash = hash_api_key(code)
                written = await eval_script(
                    r,
                    _MINT_LUA,
                    2,
                    open_key,
                    self.settings.pair_code_key(code_hash),
                    code_hash,
                    record_json,
                    str(ttl),
                    self.settings.pair_code_key_prefix,
                )
                if written:
                    expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
                    return code, expires_at
        raise RuntimeError("conversations: pair code collided twice on mint; refusing to retry further")

    async def redeem(self, code: str) -> MintingConversation:
        """Burn ``code`` and return the conversation it was minted in — an atomic ``GETDEL`` so
        exactly one caller wins under concurrency. Unknown, expired, and already-redeemed all
        raise the UNIFORM :class:`PairCodeInvalidError` (no oracle)."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            raw = await awaited(r.getdel(self.settings.pair_code_key(hash_api_key(code))))
        if raw is None:
            raise PairCodeInvalidError(_INVALID_CODE)
        return MintingConversation(**json.loads(_as_str(raw)))
