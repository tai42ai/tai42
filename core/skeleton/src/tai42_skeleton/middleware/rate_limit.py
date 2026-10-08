"""App-level rate limiter for every PUBLIC door.

Coverage is DERIVED, never listed: a route declares whether it is public
(``authed=False``) and the route registry records that, so this middleware reads
the registry and throttles every unauthenticated route it finds. A new public door
— a plugin's inbound webhook, a new login door — is flood-limited the moment it
registers, with nothing to remember to add here. Authed routes pass straight
through: the credential is the gate there.

Each request is matched against the registered surface and answered by the MOST
SPECIFIC route that covers it (longest static path prefix first, so a concrete
route always beats the Studio SPA catch-all). A request whose best match is authed,
or that matches no registered route at all, is not the limiter's business.

Doors are grouped into FAMILIES so their counters stay disjoint and a flood on one
public door cannot exhaust another's budget. A family is the door's path stem
(:func:`family_of`): ``/trigger/{token}`` and ``/universal_webhook/{topic}`` are
their own families, the six ``/api/channels/web/*`` doors share one. Every family
is charged the ``TAI_RATE_LIMIT_DEFAULT_*`` budget unless a shipped or operator
override tunes it (see :mod:`tai42_skeleton.settings.rate_limit`), so an override
tunes a door, it never decides whether the door is covered.

Keying and window semantics: a fixed-window Redis counter per client bucket,
INCR + EXPIRE issued in one pipeline (a pipeline cannot branch on INCR's result;
re-setting the TTL every hit is harmless), key TTL = 2x the window. The client
bucket comes from the shared resolver (:mod:`tai42_kit.utils.client_address`), which
believes ``X-Forwarded-For`` only under a declared proxy-trust statement and
collapses an IPv6 client to its /64.

This layer bounds REQUESTS per family. What a request then buys is the door's own
business: a channel whose doors mint conversation addresses charges a per-client
budget of its own beneath this one.

The app registers this middleware at construction, so the public surface is
rate-limited by default — a public door is exposed by design and must not ship
without its flood control.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis as AsyncRedis
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.utils.client_address import (
    XFF_HEADER,
    ClientTrustSettings,
    client_bucket,
    warn_if_proxy_trust_undeclared,
)

from tai42_skeleton.access_control.path_canon import strip_root_path
from tai42_skeleton.app.route_registry import Door, DoorTable, RouteMetadata, load_all_routes, route_registry
from tai42_skeleton.middleware.audit_log import UNAUTHENTICATED, emit_audit_line
from tai42_skeleton.settings.audit_log import audit_log_settings
from tai42_skeleton.settings.rate_limit import RateLimitSettings, rate_limit_settings

logger = logging.getLogger(__name__)

# How many leading STATIC path segments name a door family. Two is what keeps the
# families a deployment actually runs disjoint without splitting one door's own
# surface: ``/api/channels/web/*`` (page, assets, messages, stream, answer, rotate)
# folds into ``channels_web``, while ``/api/channels/slack/inbound`` stays its own
# family rather than sharing that budget.
_FAMILY_SEGMENTS = 2

# Stripped before the family stem is read: it names the transport namespace every
# ``/api`` door shares, so keeping it would spend a segment saying nothing.
_API_SEGMENT = "api"

# The family of a door whose path is parameterised from its first segment — the
# Studio SPA catch-all ``/{spa_path:path}``, which serves the shell and its assets.
_ROOT_FAMILY = "root"

# Once-per-process guard for the rate-limiting-OFF warning: the startup summary
# fires it at most once even across in-place reloads (each reload re-runs the
# summary), so a Redis-less deployment logs the warning a single time.
_RATE_LIMIT_OFF_WARNED = False

# The operator-facing warning emitted once when no Redis backs the rate limiter at
# startup-summary time — every public door is then unthrottled.
_RATE_LIMIT_OFF_WARNING = (
    "rate limiting: OFF — no Redis configured (TAI_RATE_LIMIT_REDIS_URL or TAI_DEFAULT_REDIS_URL), so EVERY "
    "public door (every route registered authed=False, whatever registered it) passes through UNTHROTTLED. "
    "Set TAI_RATE_LIMIT_REDIS_URL to enable flood control."
)


def warn_if_rate_limiting_off(log: logging.Logger) -> None:
    """Validate the trusted-proxy roster and report the limiter's boot posture.

    Constructing ``ClientTrustSettings`` first — before any dedup guard — runs its
    roster validator on every call, so a malformed ``TAI_RATE_LIMIT_TRUSTED_PROXIES``
    is refused at every boot/reload, Redis-less deployments included. When no Redis
    backs the limiter, emits the once-per-process rate-limiting-OFF warning (every
    public door then passes through); otherwise states the proxy-trust posture. The OFF
    warning is a no-op after its first firing and when Redis IS configured, so a
    Redis-less deployment warns exactly once across boots/reloads and a configured
    deployment never warns. WARNING, not a boot refusal — public doors merely go
    unthrottled.
    """
    global _RATE_LIMIT_OFF_WARNED
    # Read both fresh (not the cached singletons) so they reflect the live env at this
    # boot/reload, and before the once-per-process OFF guard so the roster validator
    # fires on every call: ``RateLimitSettings`` matches the presence gate ``_door``
    # folds on, and constructing ``ClientTrustSettings`` runs its roster validator — a
    # malformed roster is refused at every boot, Redis-less deployments included.
    settings = RateLimitSettings()
    trust = ClientTrustSettings()
    if not settings.redis.redis_url:
        if not _RATE_LIMIT_OFF_WARNED:
            _RATE_LIMIT_OFF_WARNED = True
            log.warning(_RATE_LIMIT_OFF_WARNING)
        return
    warn_if_proxy_trust_undeclared(log, trust.trusted_proxies, trust.trusted_hops)


def family_of(path: str) -> str:
    """The door family a route path belongs to: its leading STATIC path segments.

    At most :data:`_FAMILY_SEGMENTS` segments, stopping at the first ``{parameter}``, with
    the shared ``/api`` namespace segment dropped and the rest joined by ``_``.

    ``/trigger/{token}`` → ``trigger``; ``/universal_webhook/{topic}`` →
    ``universal_webhook``; ``/api/interactions/callback/{ticket}`` →
    ``interactions_callback``; every ``/api/channels/web/*`` door → ``channels_web``.
    A path parameterised from its first segment has no stem and folds into
    :data:`_ROOT_FAMILY`.
    """
    segments = [segment for segment in path.split("/") if segment]
    if segments and segments[0] == _API_SEGMENT:
        segments = segments[1:]
    stem: list[str] = []
    for segment in segments:
        if segment.startswith("{"):
            break
        stem.append(segment)
        if len(stem) == _FAMILY_SEGMENTS:
            break
    return "_".join(stem) if stem else _ROOT_FAMILY


@dataclass(frozen=True)
class _DoorFamily:
    """What the limiter needs about a matched route.

    Whether it is public, and — when it is — which family's budget it charges.
    """

    authed: bool
    family: str


def _project_family(meta: RouteMetadata) -> _DoorFamily:
    """Project every registered route into its family spec.

    Both authed and public routes are projected: an authed route must be able to out-match
    the public catch-all that also covers its path, or the limiter would throttle it.
    """
    # A route may DECLARE its family (a cost class its path-siblings do not share, e.g. an
    # upload door under /api/channels/web/*); a three-segment family is not path-derivable
    # (family_of reads at most two static segments), so the declaration is the only way in.
    # Every route that declares none derives its family from the path, unchanged.
    return _DoorFamily(authed=meta.authed, family=meta.rate_limit_family or family_of(meta.path))


# The compiled door table, memoized against the registry version that produced it: a
# reload re-records the route surface and bumps that version, so the next request
# rebuilds instead of matching against the previous deployment's doors.
_door_table_obj: DoorTable[_DoorFamily] = DoorTable(
    _project_family,
    load_routes=lambda: load_all_routes(),
    version_of=lambda: route_registry.version,
)


def _door_for(path: str, method: str) -> Door[_DoorFamily] | None:
    """The most specific registered route covering this request, or ``None`` when none matches.

    ``None`` when the registered surface holds none (an unrouted path, or one served by a
    mounted app the registry does not describe — neither is a declared public door).
    """
    return _door_table_obj.door_for(method, path)


def _reset_door_table_cache() -> None:
    """Drop the memoized table so the next request recompiles it.

    Production invalidation rides the registry version; this is for a test that swaps the
    route surface underneath the middleware without recording into the live registry.
    """
    _door_table_obj.reset()


async def _retry_after(r: AsyncRedis, prefix: str, family: str, bucket: str, limit: int, burst: int) -> int | None:
    """Fixed-window Redis limiter.

    Returns the ``Retry-After`` seconds when either window is over its limit, else
    ``None``. INCR + EXPIRE are one pipeline with EXPIRE issued UNCONDITIONALLY (a pipeline
    cannot branch on INCR's result; re-setting the TTL every hit is harmless). Key TTL =
    2x the window. The ``family`` segment keeps the public door families' counters
    disjoint.
    """
    now = time.time()
    unix_minute = int(now // 60)
    unix_10s = int(now // 10)
    m_key = f"{prefix}rl:{family}:m:{bucket}:{unix_minute}"
    s_key = f"{prefix}rl:{family}:s:{bucket}:{unix_10s}"

    pipe = r.pipeline()
    pipe.incr(m_key)
    pipe.expire(m_key, 120)
    pipe.incr(s_key)
    pipe.expire(s_key, 20)
    results = await pipe.execute()
    m_count, s_count = int(results[0]), int(results[2])

    if m_count > limit:
        return max(1, math.ceil((unix_minute + 1) * 60 - now))
    if s_count > burst:
        return max(1, math.ceil((unix_10s + 1) * 10 - now))
    return None


async def _refusal(scope: Scope) -> JSONResponse | None:
    """The 429 response for an HTTP request over its door's budget, else ``None``.

    Reads the rate-limit settings in this call only: the caller runs the downstream app
    after it returns, so a request never keeps a settings instance alive across a config
    reload that retires it.
    """
    settings = rate_limit_settings()
    # No Redis configured → rate limiting is OFF for EVERY door: the counters have
    # nowhere to live, so every request flows straight through. The
    # once-per-process boot WARNING is where an operator learns the public doors
    # are unthrottled — never a boot refusal.
    if not settings.redis.redis_url:
        return None
    # Look the door up on the root_path-stripped path — the same canonical path the
    # router (Starlette's ``get_route_path``) and access control (``strip_root_path``)
    # match on — so a deployment that carries the mount prefix in ``scope["path"]``
    # and one whose proxy strips it resolve the same registered door.
    door = _door_for(strip_root_path(scope["path"], scope.get("root_path", "")), scope["method"].upper())
    # An authed route is gated by its credential; an unrecognised path declares no
    # public door. Neither is this limiter's business.
    if door is None or door.payload.authed:
        return None
    budget = settings.budget_for(door.payload.family)
    # A disabled family means pass through, not block: an off switch opens the
    # door it names, it never closes it.
    if not budget.enabled:
        return None

    conn = HTTPConnection(scope)
    bucket = client_bucket(conn.client.host if conn.client else None, conn.headers.get(XFF_HEADER, ""))
    async with client_ctx(RedisClient, settings.redis) as r:
        retry_after = await _retry_after(
            r, settings.key_prefix, door.payload.family, bucket, budget.limit, budget.burst
        )
    if retry_after is None:
        return None
    # Refusal audit at the outer door: the limiter rejects BEFORE the gate, so
    # the caller is unauthenticated. The matched route's TEMPLATE is the whole
    # of what the line may state — a path-borne token rides in a parameter and
    # is never recorded. Duration is 0: rejected at the door. Gated on the
    # same audit switch.
    if audit_log_settings().enable:
        emit_audit_line(
            UNAUTHENTICATED,
            scope["method"],
            door.template,
            429,
            0,
            datetime.now(UTC).isoformat(),
        )
    return JSONResponse(
        {"error": "rate limit exceeded"},
        status_code=429,
        headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
    )


class RateLimitMiddleware:
    """Rate-limits every public door; passes everything else through."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the downstream ASGI ``app`` this middleware rate-limits."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer 429 when the request matches an enabled public door over its budget, else pass it through."""
        if scope["type"] == "http":
            response = await _refusal(scope)
            if response is not None:
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
