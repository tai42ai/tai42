"""The route entry gate: the explicit gate flag, hashed multi-use entry codes, and the per-client throttle."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from tai42_kit.clients.impl.redis import scan_iter

from tai42_channel_web.settings import web_settings
from tai42_channel_web.store.connection import _as_str, _now_iso, _redis

_GATE_ON = "1"
# ``secrets.token_urlsafe(16)`` -> a 22-character urlsafe code (128 bits). The raw
# code exists only in flight (mint response, visitor URL); at rest it is its hash.
_ENTRY_CODE_BYTES = 16


@dataclass(frozen=True)
class EntryCode:
    """One minted entry code as the management door lists it.

    ``code_id`` is the sha256 hex the code is stored under — the raw code is never at rest.
    """

    code_id: str
    label: str | None
    created_at: str
    expires_at: str | None


def _entry_gate_key(identity: str) -> str:
    return f"channel:web:entry_gate:{identity}"


def _entry_code_key(identity: str, code_id: str) -> str:
    return f"channel:web:entry_code:{identity}:{code_id}"


def _entry_throttle_key(client_bucket: str) -> str:
    return f"channel:web:entry_throttle:{client_bucket}"


def _code_id(raw_code: str) -> str:
    return hashlib.sha256(raw_code.encode()).hexdigest()


def _glob_escape(segment: str) -> str:
    """Backslash-escape the five Redis glob metacharacters so a SCAN ``MATCH`` treats the segment literally.

    ``_clean_identity`` admits them, and an unescaped one would match a sibling identity's keys.
    """
    for ch in ("\\", "*", "?", "[", "]"):
        segment = segment.replace(ch, "\\" + ch)
    return segment


def _entry_code_ttl(expires_at: datetime) -> int:
    """Whole seconds until ``expires_at``, refusing a past deadline — an already-dead code is never minted."""
    seconds = math.ceil((expires_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    if seconds <= 0:
        raise ValueError(f"entry code expires_at {expires_at.isoformat()} is not in the future")
    return seconds


def _decode_entry_code(code_id: str, raw: str | bytes) -> EntryCode:
    data = json.loads(raw)
    return EntryCode(code_id=code_id, label=data["label"], created_at=data["created_at"], expires_at=data["expires_at"])


async def is_gate_enabled(identity: str) -> bool:
    """Whether ``identity``'s route is gated.

    The flag is EXPLICIT: an expired or revoked code never silently reopens a route.
    """
    async with _redis() as redis:
        return await redis.get(_entry_gate_key(identity)) == _GATE_ON


async def set_gate(identity: str, enabled: bool) -> None:
    """Turn the gate on (explicit flag) or off (delete the flag)."""
    async with _redis() as redis:
        if enabled:
            await redis.set(_entry_gate_key(identity), _GATE_ON)
        else:
            await redis.delete(_entry_gate_key(identity))


async def mint_entry_code(identity: str, label: str | None, expires_at: datetime | None) -> tuple[str, str]:
    """Mint a multi-use entry code for a gated route and return ``(raw_code, code_id)``.

    Only the hash is stored; the Redis TTL is set iff ``expires_at`` (expiry IS the TTL). A past
    ``expires_at`` raises ``ValueError`` — a dead code is never minted.
    """
    ttl = _entry_code_ttl(expires_at) if expires_at is not None else None
    raw_code = secrets.token_urlsafe(_ENTRY_CODE_BYTES)
    code_id = _code_id(raw_code)
    payload = json.dumps(
        {
            "label": label,
            "created_at": _now_iso(),
            "expires_at": expires_at.astimezone(UTC).isoformat() if expires_at is not None else None,
        }
    )
    async with _redis() as redis:
        await redis.set(_entry_code_key(identity, code_id), payload, ex=ttl)
    return raw_code, code_id


async def get_entry_code(identity: str, code_id: str) -> EntryCode | None:
    """The stored record for one code id, or ``None`` when it is unknown or expired."""
    async with _redis() as redis:
        raw = await redis.get(_entry_code_key(identity, code_id))
    if raw is None:
        return None
    return _decode_entry_code(code_id, raw)


async def list_entry_codes(identity: str) -> list[EntryCode]:
    """Every live code for a route.

    SCAN over the identity prefix with the identity glob-escaped, so the ``MATCH`` cannot reach a sibling
    identity's keys; a code whose TTL lapsed between the SCAN and the GET is simply skipped.
    """
    prefix = f"channel:web:entry_code:{identity}:"
    pattern = f"channel:web:entry_code:{_glob_escape(identity)}:*"
    codes: list[EntryCode] = []
    async with _redis() as redis:
        async for key in scan_iter(redis, pattern):
            key_str = _as_str(key)
            raw = await redis.get(key_str)
            if raw is None:
                continue
            codes.append(_decode_entry_code(key_str[len(prefix) :], raw))
    return codes


async def revoke_entry_code(identity: str, code_id: str) -> bool:
    """Delete a code; ``True`` when one existed (the management door 404s otherwise)."""
    async with _redis() as redis:
        return await redis.delete(_entry_code_key(identity, code_id)) > 0


async def check_entry_code(identity: str, raw_code: str) -> bool:
    """Whether the presented code is live for ``identity``.

    Lookup is BY HASH, so a constant-time compare buys nothing: no secret is byte-compared, and presence
    of the key is liveness (expiry is the TTL).
    """
    async with _redis() as redis:
        return await redis.get(_entry_code_key(identity, _code_id(raw_code))) is not None


async def entry_attempt_allowed(client_bucket: str) -> bool:
    """Count one entry attempt against ``client_bucket``; ``False`` once the window's cap is spent.

    INCR, then EX the window on the first hit — a Redis failure raises, never fails open.
    """
    settings = web_settings()
    key = _entry_throttle_key(client_bucket)
    async with _redis() as redis:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, settings.entry_throttle_window_seconds)
    return count <= settings.entry_attempts_per_window
