"""Access-policy fetch, caching, and condition enforcement for the access-control gate."""

import asyncio
import json
import threading
from asyncio import AbstractEventLoop
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from weakref import WeakKeyDictionary

from async_lru import alru_cache
from starlette.authentication import AuthenticationError
from tai42_contract.access_control.models import AccessPolicy
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient, hgetall
from tai42_kit.settings import register_settings_reset
from tai42_kit.utils.data import run_jq_first

from tai42_skeleton.access_control.policy_version_scope import memoized_policy_version, remember_policy_version
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.access_control.store import access_control_store


class PolicyEvaluationError(Exception):
    """An INFRASTRUCTURE fault while evaluating a policy condition, distinct from a policy DENY.

    A jq timeout, a render/template fault, or an eval error — as distinct from a genuine policy
    DENY (which stays an ``AuthenticationError``).

    Deliberately NOT an ``AuthenticationError`` subclass: a caller that narrowly
    catches the deny type to fail closed (the projection build) then lets an
    infrastructure fault PROPAGATE loudly instead of silently swallowing it as a
    deny — a vanished route in an otherwise-200 projection. The runtime gate
    (``backend``/``authz``) catches broad ``Exception`` and so still fails closed on
    it.
    """


@dataclass(frozen=True)
class RenderedCondition:
    """A policy condition as enforcement evaluates it: the rendered jq ``text`` and whether one was ``configured``.

    ``configured`` is known from the policy, never from the text: a configured condition that
    renders empty must still deny, not read as "no condition".
    """

    text: str
    configured: bool


class ConditionVerdict(StrEnum):
    """What a rendered condition answers for one jq context."""

    ALLOW = "allow"
    DENY = "deny"
    RENDERED_EMPTY = "rendered_empty"


async def render_condition(condition: TemplatedText | None) -> RenderedCondition:
    """The one render-plus-configured preamble; render errors propagate as themselves."""
    if condition is None:
        return RenderedCondition(text="", configured=False)
    text = await tai42_app.storage.resource_manager.render_templated_text(condition)
    return RenderedCondition(text=text, configured=True)


def policy_is_empty(policy: AccessPolicy) -> bool:
    """Whether ``policy`` grants nothing at all — no scope and no condition.

    The ONE spelling of "this principal has no policy": an unknown or deleted key reads
    back as exactly this, so every layer that must refuse such a principal (the tokenless
    identity build, the tool edge's live re-read, the execution-key bind door, the HTTP
    backend's owner check) asks the same question and can never disagree about which
    keys exist.
    """
    return not policy.scopes and policy.condition is None


class PolicyEnforcer:
    """Fetches, caches, and enforces a principal's access policy."""

    def __init__(self, settings: AccessControlSettings):
        """Bind ``settings`` and build the version-keyed policy cache."""
        self.settings = settings

        # Cache for Policy (Static Rules). The cache is keyed on (user_id,
        # version): ``version`` is a value the fetch ignores, so a bumped version
        # simply yields a fresh cache slot — a cross-worker miss that re-reads the
        # edited policy from redis instead of serving the stale cached copy.
        self._fetch_policy = alru_cache(maxsize=settings.cache_size, ttl=settings.cache_ttl_seconds)(
            self._raw_fetch_policy_versioned
        )

    async def get_policy(self, user_id: str) -> AccessPolicy:
        """Fetch policy (scopes + rules) for a specific user (Cached).

        Reads the current policy version first (a cheap single-key GET) and mixes
        it into the cache key. In a multi-worker deployment a management edit
        bumps that version, so the stale per-worker cache entry is bypassed on the
        next read without waiting out the ttl.
        """
        return await self.get_policy_at(user_id, await self.current_policy_version())

    async def get_policy_at(self, user_id: str, version: int) -> AccessPolicy:
        """Fetch policy for a specific user at an ALREADY-READ ``version``.

        The form :meth:`get_policy` is built on, for a decision that reads several policies (a
        key's and its owner's) and then keys a further pass on the same version.

        ``version`` is a CACHE key, not a store coordinate: the fetch always reads the
        row as it stands, and a bumped version simply lands on a fresh slot. Threading
        one version through a whole decision therefore buys two things — one version
        round trip instead of several, and every cache that version keys (the policy
        cache here, the role-grant cache) answering from the same generation, so no layer
        of the decision serves a pre-bump cached copy while another serves a post-bump
        one. The underlying reads stay independent and live, which is what a fire needs.
        """
        return await self._fetch_policy(user_id, version)

    async def current_policy_version(self) -> int:
        """The current policy version (a cheap single-key GET) — the one version read every cache keys on.

        The policy cache here, the route/pattern caches of the verifier, the role-grant cache and
        the capability projection all key on it, so a writer's bump busts them together. A
        backend error fails closed by RAISING (surfaces as a clean deny), never a silent default:
        a fixed version would pin every cache to one slot and serve stale policy for the ttl. A
        successful read with no key yet is version 0.

        Inside an open request scope (:mod:`~tai42_skeleton.access_control.policy_version_scope`)
        the first successful read is remembered and every later read of the same access-control
        decision answers from it; a read that raises remembers nothing.
        """
        memoized = memoized_policy_version()
        if memoized is not None:
            return memoized
        async with client_ctx(RedisClient, self.settings.redis) as r:
            raw = await r.get(self.settings.policy_version_key)
        version = int(raw) if raw is not None else 0
        remember_policy_version(version)
        return version

    async def _raw_fetch_policy_versioned(self, user_id: str, version: int) -> AccessPolicy:
        # ``version`` participates only in the cache key (see ``__init__``); the
        # actual fetch is version-independent.
        return await self._raw_fetch_policy(user_id)

    async def _raw_fetch_policy(self, user_id: str) -> AccessPolicy:
        # A genuine backend error must fail closed by RAISING, not by returning an
        # empty policy: the alru cache only stores successful returns, so a
        # swallowed error would be cached and stick for the ttl. The error
        # propagates out of ``authenticate``, which turns it into a clean deny.
        data = await access_control_store().get_policy_body(user_id)
        # A successful read with no stored policy is legitimately empty.
        if not data:
            return AccessPolicy()
        return AccessPolicy(**data)

    async def get_live_context(self, user_id: str) -> dict[str, Any]:
        """Fetches dynamic context (usage, counters) - ALWAYS fresh from Redis.

        No caching here to ensure security enforcement is based on live data.

        The context is stored as a Redis HASH at ``ac:context:{user_id}``: each
        field is a context field name and each value is that value ``json.dumps``-
        encoded. A bare integer's JSON encoding is its plain digits, so counters an
        external metering writer maintains with ``HINCRBY ac:context:{user_id} used
        1`` are valid JSON numbers — the two writer styles compose. On read the
        per-field ``json.loads`` reassembles the real typed values (ints, objects,
        …) into a plain dict, so a jq allow-condition like
        ``.context.used < .policy.limit`` compares against a real JSON number, not a
        string.

        A malformed field value makes ``json.loads`` RAISE; it propagates out of
        ``authenticate`` and the auth decision fails closed. This is deliberate: a
        fetch/decode failure must NOT be masked as an empty context. Substituting
        ``{}`` makes a missing field read as ``null``, which can satisfy a ``<``
        comparison and flip a deny into an allow -- a real fail-open.

        A *successful* read with no stored context is different: ``HGETALL`` of a
        missing key answers ``{}``, which means there is genuinely no live data yet
        (e.g. no usage recorded), so the empty dict is the true live state and the
        condition is correctly evaluated against it.
        """
        context_key = f"{self.settings.context_prefix}{user_id}"
        async with client_ctx(RedisClient, self.settings.redis) as r:
            raw = await hgetall(r, context_key)
        return {field: json.loads(value) for field, value in raw.items()}

    @staticmethod
    async def evaluate(context: dict[str, Any], rendered: RenderedCondition) -> ConditionVerdict:
        """ALLOW when not configured or jq emits exactly True; DENY otherwise; RENDERED_EMPTY when configured and empty.

        jq/timeout errors propagate unwrapped. The evaluation runs off-loop under a wall-clock
        budget (``JQ_TIMEOUT_SECONDS``) so a hostile/buggy expression cannot block the loop.
        """
        if not rendered.text:
            return ConditionVerdict.RENDERED_EMPTY if rendered.configured else ConditionVerdict.ALLOW
        result = await run_jq_first(rendered.text, context)
        return ConditionVerdict.ALLOW if result is True else ConditionVerdict.DENY

    async def enforce(self, context: dict[str, Any], rendered: RenderedCondition) -> None:
        """Evaluate ``rendered`` against ``context``, raising ``AuthenticationError`` on a deny.

        DENY → ``AuthenticationError("Policy violation")``; RENDERED_EMPTY →
        ``AuthenticationError("Policy violation: configured condition rendered empty")`` (an empty
        render of a configured condition never fails open); any evaluation fault →
        :class:`PolicyEvaluationError`, a distinct type so a build-time caller lets it propagate
        loudly while the runtime gate's broad ``except`` still fails closed on it.
        """
        try:
            verdict = await self.evaluate(context, rendered)
        except Exception as e:
            raise PolicyEvaluationError(f"Policy error: {e!s}") from e
        if verdict is ConditionVerdict.DENY:
            raise AuthenticationError("Policy violation")
        if verdict is ConditionVerdict.RENDERED_EMPTY:
            raise AuthenticationError("Policy violation: configured condition rendered empty")


# One enforcer per running loop: its policy cache (async-lru) binds to the first loop that
# uses it. The cache also keeps that loop referenced, so a closed loop's entry is dropped
# explicitly when the next enforcer is built rather than left to the weak key.
_enforcers: "WeakKeyDictionary[AbstractEventLoop, tuple[AccessControlSettings, PolicyEnforcer]]" = WeakKeyDictionary()
_enforcers_lock = threading.Lock()


def policy_enforcer(settings: AccessControlSettings) -> PolicyEnforcer:
    """The running loop's ``PolicyEnforcer`` for ``settings``, built on first use.

    Memoized on the settings OBJECT: a different settings object gets a new enforcer, and a
    settings reset drops every one. The policy cache it carries is keyed on the policy version
    each decision reads live, so sharing it across decisions costs nothing against revocation.
    Raises ``RuntimeError`` when no event loop is running.
    """
    loop = asyncio.get_running_loop()
    with _enforcers_lock:
        held = _enforcers.get(loop)
        if held is not None and held[0] is settings:
            return held[1]
        enforcer = PolicyEnforcer(settings)
        for closed in [other for other in _enforcers if other.is_closed()]:
            del _enforcers[closed]
        _enforcers[loop] = (settings, enforcer)
        return enforcer


@register_settings_reset
def reset_policy_enforcers() -> None:
    """Drop every loop's enforcer so a settings reload rebuilds them."""
    with _enforcers_lock:
        _enforcers.clear()
