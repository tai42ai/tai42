"""Visitor session-registration records: what one cookie token stands for."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from tai42_channel_web.settings import web_settings
from tai42_channel_web.store.connection import _now_iso, _redis


class SessionRecordError(RuntimeError):
    """A stored session registration is not the shape this module wrote."""


@dataclass(frozen=True)
class SessionRegistration:
    """What one cookie token is registered as: the conversation address every door
    uses for that visitor, the web route identity the session was minted on, and the
    link params captured with the entry. A request naming a different identity is not
    this session's to serve. ``params`` is dumb transport — carried and delivered to
    the turn payload, never interpreted here."""

    visitor_id: str
    identity: str
    params: dict[str, str] = field(default_factory=dict)


def _session_key(token: str) -> str:
    return f"channel:web:session:{token}"


def _encode_session(visitor_id: str, identity: str, params: dict[str, str]) -> str:
    """The single on-disk shape both writers emit. ``params`` is ALWAYS present (an
    empty dict included); the decoder requires it, so there is no absent-key shape."""
    return json.dumps({"visitor_id": visitor_id, "identity": identity, "created_at": _now_iso(), "params": params})


def _decode_session(raw: str | bytes) -> SessionRegistration:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise SessionRecordError("a stored web session registration is not a JSON object")
    visitor_id = data.get("visitor_id")
    identity = data.get("identity")
    params = data.get("params")
    if not isinstance(visitor_id, str) or not visitor_id:
        raise SessionRecordError("a stored web session registration carries no visitor_id")
    if not isinstance(identity, str) or not identity:
        raise SessionRecordError("a stored web session registration carries no identity")
    # No old-format tolerance: a record without a str->str params map fails loud and
    # the visitor re-mints on their next navigation. A missing key is never defaulted.
    if not isinstance(params, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in params.items()):
        raise SessionRecordError("a stored web session registration carries no str-to-str params map")
    return SessionRegistration(visitor_id=visitor_id, identity=identity, params=params)


async def register_session(token: str, visitor_id: str, identity: str, params: dict[str, str]) -> None:
    """Register a freshly minted cookie token against its visitor id, the web route it
    was minted on, and the link params the entry carried. Until this lands the token is
    not a session, so it is written BEFORE the cookie is set.

    A mint gets only the SHORT pending TTL: nothing has come back with this cookie
    yet, so an anonymous mint loop leaves keys that expire in minutes rather than one
    full-TTL registration per request. ``resolve_session`` promotes it."""
    settings = web_settings()
    async with _redis() as redis:
        await redis.set(
            _session_key(token), _encode_session(visitor_id, identity, params), ex=settings.session_pending_ttl_seconds
        )


async def update_session_params(token: str, registration: SessionRegistration, params: dict[str, str]) -> None:
    """Rewrite a live visitor's captured params — same token, same visitor id, new
    params — at the FULL session TTL, because the cookie came back and this is a real
    visitor. A plain SET, so a key that expired mid-flight is simply recreated under
    the token the visitor presented; no special casing."""
    settings = web_settings()
    async with _redis() as redis:
        await redis.set(
            _session_key(token),
            _encode_session(registration.visitor_id, registration.identity, params),
            ex=settings.session_ttl_seconds,
        )


async def resolve_session(token: str) -> SessionRegistration | None:
    """The registration a cookie token stands for, promoting it to the full session
    TTL (the cookie came back, so this is a real visitor); ``None`` when the token was
    never registered or has expired — both mean "no session". One ``GETEX``: read and
    TTL in a single round trip."""
    settings = web_settings()
    async with _redis() as redis:
        raw = await redis.getex(_session_key(token), ex=settings.session_ttl_seconds)
    if raw is None:
        return None
    return _decode_session(raw)


async def drop_session(token: str) -> None:
    """Delete a registration so its token resolves to nothing (rotation)."""
    async with _redis() as redis:
        await redis.delete(_session_key(token))
