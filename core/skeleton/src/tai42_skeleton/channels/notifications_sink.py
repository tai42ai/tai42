"""The internal notifications sink — where ``notify_user(channel=None)`` writes
a message when no external channel carries it.

Storage is Redis, reusing the interactions connection
(``interactions_settings().redis``, the ``INTERACTIONS_REDIS_*`` env). The feed
key is scoped by the interactions ``key_prefix`` (default ``interactions:``) —
``{key_prefix}notifications:feed`` — exactly as :class:`InteractionStore` scopes
its own keys, so two deployments sharing one interactions Redis under distinct
``INTERACTIONS_KEY_PREFIX`` values keep separate feeds and never leak
notifications across deployments. Each notification is one JSON record — ``id``,
``message``, ``recipient``, an optional ``audience`` identity, the optional richer-send
forms ``media`` (a list of ``MediaItem`` dicts) / ``template`` (a ``ChannelTemplate``
dict) / ``options`` (a list of ``Option`` dicts) / ``schema`` (an ask-less form's
answer-schema dict, present only via the channel path's audience feed write — the sink
door refuses a channel-less form), and a server-side ``created_at`` —
pushed onto that per-deployment list; the read path returns the list newest-first — the
rich fields (``media`` alongside the message) are STORED and RETURNED by the read doors,
and rendering them is the host inbox's own surface, not a guarantee this platform makes.
``media`` is stored RAW (kept as the caller passed it, so a ``data:`` image is returned
inline under whatever CSP the host applies), never substituted to a served reference —
the shared feed carries no TTL to bound a served key's lifetime.

The feed is a bounded newest-first ring buffer: every write caps it (LTRIM) at
``interactions_settings().notifications_feed_max`` entries, keeping the newest N
and evicting older ones by design, so the feed key cannot grow without limit.
This is a deliberate, documented retention policy — an explicit product bound,
not a silent truncation of an error. The SHARED feed key carries NO TTL by
design — an unbounded lifetime, so a quiet period never drops the operator
inbox. Only the per-``audience`` feed keys carry a rolling TTL
(``interactions_settings().notifications_feed_ttl_seconds``), refreshed on
every push, so a key minted one-per-distinct-identity cannot accumulate forever
(see the per-key detail below).

``audience`` is the IDENTITY (a user_id) whose in-app inbox shows a record,
distinct from ``recipient`` (a channel delivery ADDRESS). When set, the record is
ALSO pushed onto a PER-IDENTITY feed ``{key_prefix}notifications:audience:{audience}``
(its own bounded LTRIM, mirroring the shared feed) so a restricted caller reads a
COMPLETE window of its own records that other identities' volume can never trim
out — the notifications analog of the tool-runs per-identity index. That
per-identity key also carries a rolling EXPIRE (``notifications_feed_ttl_seconds``)
refreshed on every push, so a key minted one-per-distinct-identity and read
non-destructively cannot accumulate forever — exactly as the tool-runs per-identity
index expires. The shared feed key is deliberately NOT expired: a TTL there could
drop the operator inbox after a quiet period. A broadcast (no audience) touches only
the shared feed. Post-filtering the shared bounded feed by ``audience`` is forbidden:
it would silently truncate a restricted caller's own record out of the LTRIM window
before the filter ran.

Loud by contract: a Redis failure or a malformed stored record propagates — no
swallowed error, no silent fallback. The retention cap above is the one bound the
sink applies on purpose; every error path still surfaces loudly.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, cast

from redis.asyncio import Redis
from tai42_contract.channels import ChannelTemplate, Option
from tai42_contract.interactions.models import MediaItem
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.interactions.settings import (
    INTERACTIONS_NOT_CONFIGURED_CODE,
    INTERACTIONS_NOT_CONFIGURED_MESSAGE,
    interactions_settings,
    interactions_store_configured,
)

# The sink's suffix within the shared interactions key namespace. The feed key is
# ``{key_prefix}notifications:feed`` — the interactions ``key_prefix`` threads
# per-deployment isolation through, just as it does for the interactions keys.
_FEED_SUFFIX = "notifications:feed"
# The per-identity feed suffix: ``{key_prefix}notifications:audience:{audience}``,
# the window a restricted caller reads (its own complete slice).
_AUDIENCE_FEED_SUFFIX = "notifications:audience:"


class NotificationSink:
    """Redis-backed store for internal notifications: append one record, read the
    feed newest-first. The feed key is built from the interactions ``key_prefix``
    (``{key_prefix}notifications:feed``), mirroring how
    :class:`~tai42_skeleton.interactions.store.InteractionStore` prefixes its keys,
    so per-deployment isolation on a shared Redis holds. Operations take the redis
    client as an argument (also mirroring ``InteractionStore``); each caller opens
    it from the interactions settings via
    ``client_ctx(RedisClient, interactions_settings().redis)``.

    ``max_feed_length`` is the ring-buffer bound: each :meth:`record` write LTRIMs
    the feed to this many newest entries. It is supplied at construction from
    ``interactions_settings().notifications_feed_max``.

    ``audience_feed_ttl_seconds`` is the idle TTL refreshed on the PER-AUDIENCE feed
    key on every push (from ``interactions_settings().notifications_feed_ttl_seconds``),
    so a per-identity key minted one-per-distinct-audience cannot accumulate forever.
    The shared feed key is deliberately NOT expired — a TTL there could drop the
    operator inbox after a quiet period."""

    def __init__(self, key_prefix: str, max_feed_length: int, audience_feed_ttl_seconds: int) -> None:
        self._prefix = key_prefix
        self._feed_key = f"{key_prefix}{_FEED_SUFFIX}"
        self._max_feed_length = max_feed_length
        self._audience_feed_ttl_seconds = audience_feed_ttl_seconds

    def _audience_feed_key(self, audience: str) -> str:
        return f"{self._prefix}{_AUDIENCE_FEED_SUFFIX}{audience}"

    def _queue_push_bounded(self, pipe: Any, feed_key: str, payload: str, ttl_seconds: int | None = None) -> None:
        """Queue an LPUSH of ``payload`` onto ``feed_key`` plus an LTRIM to the newest
        ``max_feed_length`` entries — the bounded newest-first ring buffer — onto
        ``pipe``. When ``ttl_seconds`` is set, a rolling EXPIRE is queued too, so a
        per-audience feed key (minted one-per-distinct-identity, read
        non-destructively) cannot accumulate forever; the shared feed passes no TTL.
        The eviction is the intended retention cap, NOT a silent truncation of an
        error."""
        pipe.lpush(feed_key, payload)
        pipe.ltrim(feed_key, 0, self._max_feed_length - 1)
        if ttl_seconds is not None:
            pipe.expire(feed_key, ttl_seconds)

    async def record(
        self,
        r: Redis,
        message: str,
        recipient: str | None,
        audience: str | None = None,
        *,
        media: list[dict] | None = None,
        template: dict | None = None,
        options: list[dict] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> dict:
        """Append one notification and return the stored record. The id and the
        ``created_at`` timestamp are minted here (server-side), never supplied by
        the caller.

        The record always lands on the shared feed (a bounded newest-first ring
        buffer). When ``audience`` is set, the SAME record is ALSO pushed onto that
        identity's per-identity feed (its own bounded ring), so a restricted caller
        reads a complete window of its own records. ``recipient`` is untouched — a
        channel delivery address, orthogonal to the ``audience`` identity.

        ``media`` / ``template`` / ``options`` / ``schema`` are the OPTIONAL richer-send forms
        the notification carried (already JSON-serialized by the caller — a list of
        ``MediaItem`` dicts, a ``ChannelTemplate`` dict, a list of ``Option`` dicts, an ask-less
        form's answer-schema dict), stored
        alongside the message and returned by the read doors — rendering them is the host
        inbox's own surface. ``None`` for each keeps the plain record shape. A ``schema``
        reaches here only from the CHANNEL path's audience feed write (feed parity with the
        delivered form); the sink door itself refuses a channel-less form notification.

        Both feed writes are issued in ONE pipeline executed once, so a failure can
        never land the record on one feed but not the other."""
        record = {
            "id": str(uuid.uuid4()),
            "message": message,
            "recipient": recipient,
            "audience": audience,
            "media": media,
            "template": template,
            "options": options,
            "schema": schema,
            "created_at": datetime.now(UTC).isoformat(),
        }
        payload = json.dumps(record)
        pipe = r.pipeline()
        self._queue_push_bounded(pipe, self._feed_key, payload)
        if audience is not None:
            # Operators still see everything on the shared feed; this extra push is
            # what makes the record complete on the addressed identity's own feed
            # regardless of others' volume. The per-identity key carries a rolling TTL
            # (the shared feed does not) so an idle identity's key cannot leak forever.
            self._queue_push_bounded(pipe, self._audience_feed_key(audience), payload, self._audience_feed_ttl_seconds)
        await pipe.execute()
        return record

    async def read(self, r: Redis) -> list[dict]:
        """Return every stored notification on the SHARED feed, newest-first. A
        malformed stored record raises out of ``json.loads`` rather than being
        skipped."""
        return await self._read_feed(r, self._feed_key)

    async def read_for(self, r: Redis, audience: str) -> list[dict]:
        """Return the records on ``audience``'s per-identity feed, newest-first — the
        complete window a restricted caller reads, never truncated by other
        identities' volume (never a post-filter over the shared feed)."""
        return await self._read_feed(r, self._audience_feed_key(audience))

    async def _read_feed(self, r: Redis, feed_key: str) -> list[dict]:
        # redis-py's async stubs type ``lrange`` with the sync (non-awaitable)
        # return; at runtime it is awaitable, so the cast bridges the stub.
        raw_items = await cast("Awaitable[list[str]]", r.lrange(feed_key, 0, -1))
        return [json.loads(item) for item in raw_items]


async def record_notification(
    message: str,
    recipient: str | None = None,
    audience: str | None = None,
    *,
    media: list[MediaItem] | None = None,
    template: ChannelTemplate | None = None,
    options: list[Option] | None = None,
    schema: dict[str, Any] | None = None,
) -> dict:
    """Write one notification to the internal sink and return the stored record.

    Opens the interactions Redis connection and delegates to
    :meth:`NotificationSink.record`. When ``audience`` is set the record also lands
    on that identity's per-identity feed. ``media``/``template``/``options``/``schema`` are
    the OPTIONAL richer-send forms (already contract-validated by the caller); they are
    JSON-serialized here and stored on the record (``schema`` is stored as the plain dict it
    already is). Every Redis or serialization failure
    propagates loudly.
    """
    # The in-app feed is interactions-Redis-backed, so every feed write refuses with
    # the feature's named 501 when the store is unconfigured — this is the sole writer
    # of the feed, so no path reaches an absent store and opaquely 500s. The
    # NotSupportedError import is function-local: the operations package pulls in the app
    # surface, which imports back through this channels module, so a module-level import
    # would cycle.
    from tai42_skeleton.operations import NotSupportedError

    if not interactions_store_configured():
        raise NotSupportedError(INTERACTIONS_NOT_CONFIGURED_MESSAGE, extra={"code": INTERACTIONS_NOT_CONFIGURED_CODE})
    settings = interactions_settings()
    sink = NotificationSink(
        settings.key_prefix, settings.notifications_feed_max, settings.notifications_feed_ttl_seconds
    )
    async with client_ctx(RedisClient, settings.redis) as r:
        return await sink.record(
            r,
            message,
            recipient,
            audience=audience,
            media=[item.model_dump(mode="json") for item in media] if media is not None else None,
            template=template.model_dump(mode="json") if template is not None else None,
            options=[option.model_dump(mode="json") for option in options] if options is not None else None,
            schema=schema,
        )


async def read_notifications(audience: str | None = None) -> list[dict]:
    """Return the internal sink's notifications, newest-first.

    Opens the interactions Redis connection and delegates to
    :meth:`NotificationSink.read` (the shared feed) or, when ``audience`` is set,
    :meth:`NotificationSink.read_for` (that identity's per-identity feed). Every
    Redis or serialization failure propagates loudly.
    """
    settings = interactions_settings()
    sink = NotificationSink(
        settings.key_prefix, settings.notifications_feed_max, settings.notifications_feed_ttl_seconds
    )
    async with client_ctx(RedisClient, settings.redis) as r:
        if audience is not None:
            return await sink.read_for(r, audience)
        return await sink.read(r)
