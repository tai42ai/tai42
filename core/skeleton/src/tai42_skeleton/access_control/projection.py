"""The derived capability projection — what the caller can actually reach.

``GET /api/auth/me`` answers this projection: the concrete (path, method) surface,
dynamic route patterns, sub-MCP mounts, tools, and agents an authenticated caller can
reach RIGHT NOW, derived — never stored, never a second ACL. The one invariant is
**projection ⊆ gate**: every projected surface is one the real middleware + backend
stack would ADMIT, so the projection can never advertise a door the gate would slam.

How the invariant is held:

- **Scopes are READ, never recomputed** — ``build_projection`` consumes the effective
  (owner-attenuated) scopes the backend already committed to the request, so the
  projection filters against the SAME scope set the edge enforces.
- **Reachability is the gate's own resolution** — a route declaring
  ``any_authenticated`` short-circuits BEFORE resolution (exactly as
  ``ResourceGuardMiddleware`` checks it); otherwise every candidate path is resolved through
  :meth:`AccessControlVerifier.resolve_resource_ids` and coverage-checked exactly as the
  middleware does (deny wins: ALL resolved protected ids covered, or ``"*"``).
- **jq is exact, per (path, method)** — every reachable candidate is evaluated through
  the REAL :class:`PolicyEnforcer` over the SAME passes the backend builds
  (:func:`~tai42_skeleton.access_control.standing.jq_passes`: the key's condition, then the
  owner's condition for an owned key) on the SAME canonical probe path, so a jq fence that
  denies a route at the edge denies it in the projection too. An admin (the condition-free
  ``"*"`` discriminator) skips the jq pass — its policy carries no condition by
  definition.

**Point-in-time:** both ``.context.*`` (a single ``get_live_context`` read) and
``.system.time`` (a single ``time.time()`` read, baked into every pass when the passes are
built) are snapshotted once per build, so a cached projection reports them
as of build time and is point-in-time within the ttl. This is informational only: the
enforcement gate evaluates ``.context.*`` and ``.system.time`` fresh per request, so a
condition whose truth turns on live time can read stale in the projection yet is always
evaluated live at the gate.

**Failure doctrine:** any store/registry/render/jq INFRASTRUCTURE error propagates
(surfaces as a 500) — never a partial projection. A caller's own condition legitimately
DENYING a candidate is not an error (it is exactly what the gate does) and simply omits
that candidate.

**Cache:** keyed on ``(user_id, policy_version, sorted(effective_scopes),
claims_digest)`` — fuller than the policy cache's ``(user_id, version)`` because the
projection also depends on the caller's effective scopes and identity claims, neither
reconstructable from ``user_id`` alone. Every scope/route/policy mutation bumps the
version, so a route-table edit invalidates projections with no new machinery.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from async_lru import alru_cache
from pydantic import BaseModel
from starlette.authentication import AuthenticationError
from tai42_contract.access_control import UNIVERSAL_SCOPE
from tai42_contract.app import tai42_app
from tai42_kit.settings import register_settings_reset

from tai42_skeleton.access_control import management
from tai42_skeleton.access_control.coverage import is_public_only, scopes_cover
from tai42_skeleton.access_control.path_canon import canonicalize_path
from tai42_skeleton.access_control.policy import PolicyEnforcer, RenderedCondition, policy_enforcer, render_condition
from tai42_skeleton.access_control.projection_pattern_sampling import _sample_path_for_pattern
from tai42_skeleton.access_control.role_gate import declares_any_authenticated
from tai42_skeleton.access_control.role_grants import role_level_decision
from tai42_skeleton.access_control.settings import AccessControlSettings, access_control_settings
from tai42_skeleton.access_control.standing import JqPass, Standing, StandingDenied, jq_passes, resolve_standing
from tai42_skeleton.access_control.store import access_control_store
from tai42_skeleton.access_control.verifier import AccessControlVerifier
from tai42_skeleton.app.route_registry import RouteMetadata, load_api_routes
from tai42_skeleton.app.sub_mcp_app import sub_mcp_access_pattern, sub_mcp_mount_url
from tai42_skeleton.operations.errors import PermissionDeniedError
from tai42_skeleton.routers.paths import RUN_TOOL_PATH, TOOL_RUNS_PATH, agent_run_path
from tai42_skeleton.sub_mcp.store import get_sub_mcp_store

logger = logging.getLogger(__name__)

# The synthetic caller id used when the gate is OFF: there is no principal to project,
# so the route returns a total projection under this named identity.
NO_AUTH_USER_ID = "__no_auth__"

# The global tool-execution doors: iff the caller can reach one of these, the whole
# registry tool surface is projected (there is no per-tool ACL).
_TOOL_RUN_DOORS: frozenset[tuple[str, str]] = frozenset({("POST", RUN_TOOL_PATH), ("POST", TOOL_RUNS_PATH)})


# -- Response models ---------------------------------------------------------


class RouteEntry(BaseModel):
    """A concrete route the caller can reach, with the methods that pass its jq."""

    path: str
    methods: list[str]


class PatternEntry(BaseModel):
    """A dynamic route pattern the caller can reach.

    A mount/pattern surface that is NOT enumerable into concrete paths, projected
    only when its scope AND jq admit it.
    """

    pattern: str
    scope_id: str


class SubMcpEntry(BaseModel):
    """A sub-MCP mount the caller can reach, with the served URL and access pattern a route row maps it by."""

    slug: str
    tools: list[str]
    transport: str
    mount_url: str
    access_pattern: str


class PrincipalRef(BaseModel):
    """The principal a credential belongs to: the owner of a key, or the human of a session."""

    user_id: str
    kind: str
    display_name: str


class ProjectionResult(BaseModel):
    """The caller's derived capability projection — every field derived, never stored."""

    user_id: str
    owner_user_id: str | None
    principal: PrincipalRef | None
    admin: bool
    # The reserved route-table marker a public route is mapped to (never a scope).
    public_resource_id: str
    scopes: list[str]
    routes: list[RouteEntry]
    route_patterns: list[PatternEntry]
    sub_mcp: list[SubMcpEntry]
    tools: list[str]
    agents: list[str]
    mintable: bool


# -- Live-source seams (monkeypatch points for unit tests) -------------------


def _registry_routes() -> list[RouteMetadata]:
    """Every registered ``/api/*`` route's (path, methods) metadata."""
    return load_api_routes()


async def _sub_mcp_routes() -> dict[str, Any]:
    """The durable sub-MCP registrations as ``{slug: RouteConfig}``.

    Coherent across workers, not this worker's in-process cache.
    """
    return await get_sub_mcp_store().list_routes()


async def _all_registry_tools() -> list[str]:
    """Every registered tool name."""
    return sorted((await tai42_app.tools.get_tools()).keys())


def _all_agent_names() -> list[str]:
    """Every registered agent name."""
    from tai42_skeleton.app import instance

    return sorted(instance.app.agents.all_agents().keys())


# -- claims digest + frozen wrapper (the cache mechanism) --------------------


def _claims_digest(claims: Mapping[str, Any]) -> str:
    """Collapse the whole claims mapping into one hashable token for the cache key.

    So the cache key captures EVERYTHING a jq ``.identity.*`` condition could read (not
    just the owner claim) and can never serve a stale or wrong-identity projection. A
    non-serializable claim value RAISES loudly (the failure doctrine), never a silent
    digest of a partial view.
    """
    canonical = json.dumps(dict(claims), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _FrozenClaims:
    """A frozen wrapper carrying the full (unhashable) claims into the cached body.

    Hashes and compares ONLY on the claims digest.

    ``alru_cache`` hashes every argument, so the raw claims dict cannot be a cache arg;
    this wrapper keys solely on the digest (which already captures the whole claims), so
    the cached body reads ``wrapper.claims`` to build the jq contexts without the dict
    itself entering the hash.
    """

    __slots__ = ("claims", "digest")

    def __init__(self, claims: Mapping[str, Any], digest: str) -> None:
        self.claims = claims
        self.digest = digest

    def __hash__(self) -> int:
        return hash(self.digest)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FrozenClaims) and self.digest == other.digest


class _FrozenScopes:
    """Carries the ORIGINAL-order effective scopes into the cached build.

    Hashes and compares ONLY on the SORTED tuple.

    The cache key must stay order-independent (a given caller's effective-scope order is
    a deterministic function of its policy + version, so two order-variants of one set
    are the same slot), yet the build must evaluate jq against the SAME scope ORDER the
    backend gate does — the backend enforces over ``resolved_scopes`` in original order,
    so an order-sensitive condition (``.scopes[0]``) would otherwise diverge between
    projection and gate. This wrapper keys on the sorted tuple but hands the build the
    unsorted list.
    """

    __slots__ = ("_key", "scopes")

    def __init__(self, scopes: list[str]) -> None:
        self.scopes = scopes
        self._key = tuple(sorted(scopes))

    def __hash__(self) -> int:
        return hash(self._key)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FrozenScopes) and self._key == other._key


# -- gate-faithful reachability + jq -----------------------------------------


async def _path_reachable(
    verifier: AccessControlVerifier,
    settings: AccessControlSettings,
    scope_set: set[str],
    version: int,
    path: str,
    method: str,
) -> bool:
    """Whether ``(path, method)`` clears the route-resolution + scope-coverage gate or is any-authenticated.

    This is the SAME decision ``ResourceGuardMiddleware`` reaches, jq excluded (jq is a
    separate per-method pass). The ``method`` is threaded so the declared-protection tier
    resolves a registered authenticated surface to the universal scope per method, keeping
    projection ⊆ gate exact.
    """
    # A route declaring ``any_authenticated`` is admitted BEFORE resolution, exactly as the
    # middleware does, so one that ALSO carries a route row is not under-shown by falling
    # through to a scope-coverage test the middleware never reaches.
    if declares_any_authenticated(path, method):
        return True
    ids = await verifier.resolve_resource_ids(path, method=method, policy_version=version)
    if not ids:
        # No resolved id and not carved: unreachable.
        return False
    public = settings.public_resource_id
    return is_public_only(ids, public) or scopes_cover(ids, scope_set, public)


async def _jq_admits(
    enforcer: PolicyEnforcer,
    passes: list[tuple[JqPass, RenderedCondition]],
    path: str,
    method: str,
) -> bool:
    """Whether every condition pass (the key's, then the owner's for an owned key) admits the canonical probe.

    The SAME passes the backend enforces, so a fenced route is denied here exactly as it is at
    the edge. A genuine policy DENY returns ``False``; a jq/render/store INFRASTRUCTURE fault (a
    ``PolicyEvaluationError``, which is NOT an ``AuthenticationError``) propagates loudly rather
    than being swallowed as a deny that would silently drop the route from a 200 projection.
    """
    try:
        for jq_pass, rendered in passes:
            await enforcer.enforce(jq_pass.context_for(method, path), rendered)
    except AuthenticationError:
        return False
    return True


# -- cache -------------------------------------------------------------------


_CachedBuilder = Callable[[str, int, _FrozenScopes, _FrozenClaims], Awaitable[ProjectionResult]]
_cached_builder: _CachedBuilder | None = None


def _get_cached_builder(settings: AccessControlSettings) -> _CachedBuilder:
    """The memoized ``alru_cache``-wrapped builder, mirroring ``PolicyEnforcer``'s cache.

    Same ``cache_size`` / ``cache_ttl_seconds`` bound. Version participates in the key, so
    a mutation-driven version bump yields a fresh slot — a cross-worker miss.
    """
    global _cached_builder
    if _cached_builder is None:
        _cached_builder = alru_cache(maxsize=settings.cache_size, ttl=settings.cache_ttl_seconds)(_build_uncached)
    return _cached_builder


@register_settings_reset
def reset_projection_cache() -> None:
    """Drop the memoized builder so a fresh settings object (or a test) rebuilds it.

    Registered with the settings-reset registry so a config reload (which runs
    ``reset_all_settings()``) rebuilds it against the new ``cache_size`` /
    ``cache_ttl_seconds`` bound instead of serving from a builder bound to the stale
    settings snapshot, mirroring the sibling ``@register_settings_reset`` caches.
    """
    global _cached_builder
    _cached_builder = None


# -- the build ---------------------------------------------------------------


async def build_projection(user_id: str, effective_scopes: list[str], claims: Mapping[str, Any]) -> ProjectionResult:
    """The caller's capability projection (cached, version-keyed).

    ``user_id``, ``effective_scopes``, and ``claims`` come from the request; the caller's
    policy — and, for an owned key, the owner's policy — are fetched INTERNALLY through
    the version-keyed policy cache, so the handler never fetches policy itself.
    """
    settings = access_control_settings()
    version = await policy_enforcer(settings).current_policy_version()
    wrapper = _FrozenClaims(dict(claims), _claims_digest(claims))
    builder = _get_cached_builder(settings)
    return await builder(user_id, version, _FrozenScopes(list(effective_scopes)), wrapper)


async def _build_uncached(
    user_id: str, version: int, scopes: _FrozenScopes, wrapper: _FrozenClaims
) -> ProjectionResult:
    settings = access_control_settings()
    # ORIGINAL-order scopes (the wrapper keys the cache on the sorted tuple, but jq must
    # evaluate the exact order the gate does — see ``_FrozenScopes``).
    effective_scopes = scopes.scopes
    scope_set = set(effective_scopes)
    claims = wrapper.claims

    enforcer = policy_enforcer(settings)
    verifier = AccessControlVerifier(settings, providers=[])

    # The owner is the one the STORED policy names — the owner every door reads — and the
    # request's verified owner claim is asserted equal to it, so the projection classifies the
    # caller byte-identically to the gate. The request was admitted by the backend at an earlier
    # version; a principal that lost its standing since is a loud refusal, never a projection.
    try:
        standing = await resolve_standing(enforcer, user_id, version=version, verified_claims=claims)
    except StandingDenied as denied:
        logger.error(  # noqa: TRY400 a refusal outcome with its reason, not an unexpected error — no traceback
            "access_control: projection refused for %s — %s", user_id, denied
        )
        raise PermissionDeniedError("access denied: principal standing changed during the request") from denied
    owner_claim = standing.owner
    admin = standing.is_admin

    principal = await _resolve_principal(user_id, owner_claim)

    # One point-in-time live-context read for every jq pass in this build.
    live_ctx = await enforcer.get_live_context(user_id)

    admits = await _build_admits(enforcer, standing, effective_scopes, claims, live_ctx, user_id, version)

    routes, projected_pairs = await _project_routes(verifier, settings, scope_set, version, admits)
    route_patterns = await _project_route_patterns(verifier, settings, scope_set, version, admits)
    sub_mcp = await _project_sub_mcp(verifier, settings, scope_set, version, admits)
    tools = await _project_tools(projected_pairs, sub_mcp)
    agents = await _project_agents(verifier, settings, scope_set, version, admits)

    return ProjectionResult(
        user_id=user_id,
        owner_user_id=owner_claim,
        principal=principal,
        admin=admin,
        public_resource_id=settings.public_resource_id,
        scopes=effective_scopes,
        routes=routes,
        route_patterns=route_patterns,
        sub_mcp=sub_mcp,
        tools=tools,
        agents=agents,
        mintable=_mintable(),
    )


async def _resolve_principal(user_id: str, owner_claim: str | None) -> PrincipalRef | None:
    """The principal the caller's credential belongs to.

    A KEY (``owner_claim`` set) belongs to its OWNER principal; its absence is an invariant
    breach (every key belongs to a principal), raised loudly. A SESSION / top-level
    principal (no owner claim) IS the human — its own ``user_id`` names the principal;
    ``None`` only when no principal row exists for it.
    """
    store = access_control_store()
    if owner_claim is not None:
        row = await store.get_principal(owner_claim)
        if row is None:
            raise RuntimeError(
                f"access_control: api key {user_id!r} names owner principal {owner_claim!r} "
                "that has no principal row — ownerless credential; identity invariant broken"
            )
        return PrincipalRef(user_id=row["user_id"], kind=row["kind"], display_name=row["display_name"])
    row = await store.get_principal(user_id)
    if row is None:
        return None
    return PrincipalRef(user_id=row["user_id"], kind=row["kind"], display_name=row["display_name"])


async def _build_admits(
    enforcer: PolicyEnforcer,
    standing: Standing,
    effective_scopes: list[str],
    claims: Mapping[str, Any],
    live_ctx: dict[str, Any],
    user_id: str,
    version: int,
) -> Callable[[str, str], Awaitable[bool]]:
    """Build the ``admits(path, method)`` predicate the request gate runs, so ``projection ⊆ gate`` holds.

    The predicate is the jq passes ∧ per-tag LEVEL decision the request gate runs, over a
    canonical probe path. Each pass's condition is rendered ONCE per build (it is invariant
    across every probe — only ``.request`` varies), then reused for every probe.
    """
    passes: list[tuple[JqPass, RenderedCondition]] = []
    if not standing.is_admin:
        built = jq_passes(
            standing, user_id=user_id, claims=claims, live_context=live_ctx, scopes=effective_scopes, now=time.time()
        )
        passes.extend([(jq_pass, await render_condition(jq_pass.condition)) for jq_pass in built])

    async def admits(path: str, method: str) -> bool:
        if standing.is_admin:
            return True
        if not await _jq_admits(enforcer, passes, path, method):
            return False
        # The per-tag LEVEL term — the SAME shared decision the request gate runs, so a
        # fenced route or an ungranted tag is omitted here exactly as it is denied at the
        # edge (projection ⊆ gate). A missing-role pointer denies; an infra fault
        # propagates per the projection's failure doctrine.
        allowed, _cause = await role_level_decision(standing.policy, standing.owner_policy, path, method, version)
        return allowed

    return admits


async def _project_routes(
    verifier: AccessControlVerifier,
    settings: AccessControlSettings,
    scope_set: set[str],
    version: int,
    admits: Callable[[str, str], Awaitable[bool]],
) -> tuple[list[RouteEntry], set[tuple[str, str]]]:
    """Every registry route whose resolution+scope gate admits it, then jq-filtered per method.

    Returns the entries and the projected ``(method, path)`` pairs.
    """
    routes: list[RouteEntry] = []
    projected_pairs: set[tuple[str, str]] = set()
    for meta in _registry_routes():
        # A templated registry path (``/api/agents/{name}/runs``) is a dynamic surface, not
        # a concrete route: ``resolve_resource_ids`` would ``fullmatch`` the brace-literal
        # under a dynamic-pattern row and emit a bogus RouteEntry with literal braces that
        # also double-lists a surface the route_patterns loop already carries. Such routes
        # are represented ONLY via route_patterns (and the sub_mcp/agents lists).
        if "{" in meta.path:
            continue
        probe = canonicalize_path(meta.path)
        allowed = [
            method
            for method in meta.methods
            if await _path_reachable(verifier, settings, scope_set, version, probe, method)
            and await admits(probe, method)
        ]
        if allowed:
            routes.append(RouteEntry(path=meta.path, methods=sorted(allowed)))
            projected_pairs.update((method, meta.path) for method in allowed)
    routes.sort(key=lambda entry: entry.path)
    return routes, projected_pairs


async def _project_route_patterns(
    verifier: AccessControlVerifier,
    settings: AccessControlSettings,
    scope_set: set[str],
    version: int,
    admits: Callable[[str, str], Awaitable[bool]],
) -> list[PatternEntry]:
    """Dynamic route patterns, scope- AND jq-filtered exactly like routes via a representative path.

    A pattern with no derivable representative is excluded (logged), never leaked.
    """
    patterns = await management.get_all_existing_patterns()
    mappings = await management.get_all_route_mappings()
    route_patterns: list[PatternEntry] = []
    for template_url, regex in sorted(patterns.items()):
        scope_id = mappings.get(template_url)
        if scope_id is None:
            continue
        sample = _sample_path_for_pattern(regex)
        if sample is None:
            logger.info("access_control: projection excluding non-sampleable route pattern %r", regex)
            continue
        representative = canonicalize_path(sample)
        if not await _path_reachable(verifier, settings, scope_set, version, representative, "GET"):
            continue
        if not await admits(representative, "GET"):
            continue
        route_patterns.append(PatternEntry(pattern=regex, scope_id=scope_id))
    return route_patterns


async def _project_sub_mcp(
    verifier: AccessControlVerifier,
    settings: AccessControlSettings,
    scope_set: set[str],
    version: int,
    admits: Callable[[str, str], Awaitable[bool]],
) -> list[SubMcpEntry]:
    """Sub-MCP mounts, scope- AND jq-filtered exactly like every other surface.

    Coverage on the mount root the gate resolves PLUS a jq GET-probe of the mount root, so
    a mount whose jq condition denies it is not topology-leaked. Only a mount admitted by
    BOTH is projected, and only its tools fold into the tool union.
    """
    sub_mcp: list[SubMcpEntry] = []
    sub_routes = await _sub_mcp_routes()
    for slug in sorted(sub_routes):
        config = sub_routes[slug]
        mount_root = canonicalize_path(sub_mcp_mount_url(slug))
        if not await _path_reachable(verifier, settings, scope_set, version, mount_root, "GET"):
            continue
        if not await admits(mount_root, "GET"):
            continue
        sub_mcp.append(
            SubMcpEntry(
                slug=slug,
                tools=list(config.tools),
                transport=config.transport,
                mount_url=sub_mcp_mount_url(slug),
                access_pattern=sub_mcp_access_pattern(slug),
            )
        )
    return sub_mcp


async def _project_tools(projected_pairs: set[tuple[str, str]], sub_mcp: list[SubMcpEntry]) -> list[str]:
    """Every registry tool iff a global tool-run door is projected; otherwise the union of allowed mounts' tools.

    No per-tool ACL exists or is invented.
    """
    if any(door in projected_pairs for door in _TOOL_RUN_DOORS):
        return await _all_registry_tools()
    tool_names: set[str] = set()
    for entry in sub_mcp:
        tool_names.update(entry.tools)
    return sorted(tool_names)


async def _project_agents(
    verifier: AccessControlVerifier,
    settings: AccessControlSettings,
    scope_set: set[str],
    version: int,
    admits: Callable[[str, str], Awaitable[bool]],
) -> list[str]:
    """Each agent whose per-agent run door passes the gate (resolution + jq POST).

    A path-specific jq fence therefore projects per-agent truthfully.
    """
    agents: list[str] = []
    for name in _all_agent_names():
        run_path = canonicalize_path(agent_run_path(name))
        if await _path_reachable(verifier, settings, scope_set, version, run_path, "POST") and await admits(
            run_path, "POST"
        ):
            agents.append(name)
    return agents


def _mintable() -> bool:
    """Whether any configured identity provider can mint keys, independent of ``settings.enable``.

    A gate-off deployment whose provider physically cannot mint reports ``False``.
    """
    return any(mintable for _name, mintable in management.provider_capabilities())


def synthetic_full_projection() -> ProjectionResult:
    """The gate-OFF total projection: with no identity to project, every surface is reachable.

    ``admin=True`` + the universal scope under the named ``__no_auth__`` identity; the list
    fields are explicitly EMPTY (the Studio renders everything off the full-projection
    flag). ``mintable`` is still DERIVED — a provider that physically cannot mint reports
    ``False`` even here.
    """
    return ProjectionResult(
        user_id=NO_AUTH_USER_ID,
        owner_user_id=None,
        principal=None,
        admin=True,
        public_resource_id=access_control_settings().public_resource_id,
        scopes=[UNIVERSAL_SCOPE],
        routes=[],
        route_patterns=[],
        sub_mcp=[],
        tools=[],
        agents=[],
        mintable=_mintable(),
    )
