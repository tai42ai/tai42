"""The single tool-edge authorization entry point.

``check`` applies the HTTP edge's terms — route→resource verifier, policy/jq fences,
per-tag LEVEL decision — to the path SYNTHESIZED from the operation's route template
plus the call's path arguments, in the same fail-closed conjunction. Raises
:class:`PermissionDeniedError` on a deny, returns on an allow, never grants on an error.

Path arguments are caller-supplied, so the synthesized path is pinned twice before any
layer reads it: substitution refuses a value that does not fill the segment(s) its
parameter declares, and the result must resolve back to the operation's OWN registered
route.

Unlike ``ResourceGuardMiddleware`` CASE A, an operation with no configured resource row
is denied for EVERY caller, super-admins included: the tool edge is never easier than
the route.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jinja2 import TemplateError
from starlette.authentication import AuthenticationError
from tai42_contract.template import TemplatedText
from tai42_kit.settings import register_settings_reset

from tai42_skeleton.access_control.coverage import is_public_only, scopes_cover
from tai42_skeleton.access_control.path_canon import MalformedPathError, canonicalize_path
from tai42_skeleton.access_control.policy import PolicyEnforcer, RenderedCondition, policy_enforcer, render_condition
from tai42_skeleton.access_control.role_gate import resolve_route_meta
from tai42_skeleton.access_control.role_grants import role_level_decision_for_route
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.access_control.standing import (
    Standing,
    StandingDenied,
    StandingDenyReason,
    jq_passes,
    resolve_standing,
)
from tai42_skeleton.access_control.verifier import AccessControlVerifier, is_always_public_prefix
from tai42_skeleton.authz.execution_identity import get_execution_identity
from tai42_skeleton.authz.identity import CallerIdentity
from tai42_skeleton.authz.token_free import TokenFreeConditionError, assert_token_free_evaluable
from tai42_skeleton.operations.errors import PermissionDeniedError
from tai42_skeleton.template import TemplateNotFoundError

if TYPE_CHECKING:
    from tai42_contract.access_control.models import AccessPolicy

    from tai42_skeleton.access_control.settings import AccessControlSettings
    from tai42_skeleton.app.route_registry import RouteMetadata
    from tai42_skeleton.operations.registry import OperationMetadata

logger = logging.getLogger(__name__)

# Route-template parameter: name, plus declared converter (``{id}`` → None).
_PATH_PARAM = re.compile(r"\{([^}:]+)(?::([^}]+))?\}")

# The only converter whose value may span several path segments; any other names exactly one.
_MULTI_SEGMENT_CONVERTER = "path"

# Segments a path argument may never contribute: they collapse or re-parent the path.
_UNSAFE_SEGMENTS = frozenset({"", ".", ".."})


def synthesize_path(op: OperationMetadata, call_arguments: dict[str, object]) -> str:
    """Build the concrete resource path for ``op`` by substituting the call's path args into the route template.

    The result is the SAME canonical form the HTTP edge derives from the raw request
    target (each segment decoded once, ``/`` re-encoded to ``%2F`` and ``%`` to ``%25``).

    A plain ``{name}`` value that carries ``/`` (a state record ``{key}`` — a thread key
    carries ``/``) is a single addressed segment: it is re-encoded so the slash stays
    INSIDE the segment, exactly as the raw-path record doors keep it, never split across
    segments. A ``{name:path}`` value keeps its ``/`` as separators. Either way no value
    may contribute an empty or ``.``/``..`` segment (checked per decoded segment);
    anything else raises :class:`PermissionDeniedError`.
    """
    if op.route_template is None:
        raise ValueError(f"operation {op.name!r} has no route template; it was never registered as a route")

    def _sub(match: re.Match[str]) -> str:
        param = match.group(1)
        if param not in call_arguments:
            raise PermissionDeniedError(f"access denied: missing path argument {param!r} for {op.name!r}")
        value = str(call_arguments[param])
        if any(segment in _UNSAFE_SEGMENTS for segment in value.split("/")):
            raise PermissionDeniedError(f"access denied: path argument {param!r} for {op.name!r} is not a path segment")
        if match.group(2) == _MULTI_SEGMENT_CONVERTER:
            # A ``:path`` parameter's ``/`` are genuine separators, not data.
            return value
        # A plain segment: re-encode reversibly so a data slash stays one segment, matching
        # the canonical form ``request_canonical_path`` builds from the raw request path.
        return value.replace("%", "%25").replace("/", "%2F")

    return _PATH_PARAM.sub(_sub, op.route_template)


def _own_route(op: OperationMetadata, path: str, method: str) -> RouteMetadata:
    """Return the registered route ``op`` dispatches as, asserting ``path`` resolves to it.

    Denies if ``path`` resolves to no route or a different one.

    Resolved ONCE here and reused by every term below: re-resolving the caller-influenced
    path per term could let terms disagree on which route is authorized, and an
    unresolvable path reads as "not gated" to the per-tag decision, dropping the fence.
    """
    template = op.route_template
    if template is None:
        raise AssertionError
    try:
        canonical = canonicalize_path(path)
        # A raise here (an encoded slash resolving to a non-raw route) is a fail-closed
        # deny, not a route: authz would otherwise reason on a form the router never serves.
        meta = resolve_route_meta(canonical, method)
    except MalformedPathError as exc:
        raise PermissionDeniedError(
            f"access denied: {method} {path} is not a well-formed path for {op.name!r}"
        ) from exc
    if meta is None or canonicalize_path(meta.path) != canonicalize_path(template):
        raise PermissionDeniedError(
            f"access denied: {method} {path} does not resolve to the route {op.name!r} is registered at"
        )
    return meta


def _assert_execution_condition_evaluable(condition: str, *, principal: str, template_id: str | None) -> None:
    """Deny unless a RENDERED policy condition is evaluable under a background execution's reduced claim set.

    Must be asserted on the rendered text about to be enforced, not only at bind time: a
    template edit changes that text with no write to the bound record.
    """
    try:
        assert_token_free_evaluable(condition)
    except TokenFreeConditionError as exc:
        logger.warning(
            "authz: background execution denied — the policy condition enforced for %s (template %r) is not "
            "token-free-evaluable: %s",
            principal,
            template_id,
            exc,
        )
        raise PermissionDeniedError(
            f"access denied: policy condition for {principal!r} is not evaluable at a fire"
        ) from exc


async def _render_condition(condition: TemplatedText | None, *, principal: str) -> RenderedCondition:
    """``condition`` as ``enforce`` will evaluate it.

    A render failure is a typed refusal naming the principal — never read as "no
    condition" and never flattened into the generic catch-all, which would drop the very
    fences this decision applies. The render error's own text is logged, not answered: it
    can quote template content.
    """
    try:
        return await render_condition(condition)
    except (ValueError, TemplateError, TemplateNotFoundError) as exc:
        logger.warning(
            "authz: denied — the policy condition of %s (template %r) does not render: %s",
            principal,
            condition.id if condition is not None else None,
            exc,
        )
        raise PermissionDeniedError(f"access denied: the policy condition of {principal!r} does not render") from exc


_verifier: tuple[AccessControlSettings, AccessControlVerifier] | None = None


def _tool_edge_verifier(settings: AccessControlSettings) -> AccessControlVerifier:
    """The ONE verifier this edge resolves routes through, memoized per settings object.

    Its route/pattern caches are keyed on the policy version each decision reads live, so
    reuse costs nothing against revocation; a fresh instance per dispatch would make them
    dead. Memo is keyed on the settings OBJECT — a different settings object gets its own
    verifier. Only the route→resource map is shared; policy/context/grant reads stay
    per-decision.
    """
    global _verifier
    if _verifier is None or _verifier[0] is not settings:
        _verifier = (settings, AccessControlVerifier(settings))
    return _verifier[1]


@register_settings_reset
def reset_tool_edge_verifier() -> None:
    """Drop the memoized tool-edge verifier so a settings reload rebuilds it."""
    global _verifier
    _verifier = None


async def check(
    caller_identity: CallerIdentity,
    operation_metadata: OperationMetadata,
    call_arguments: dict[str, object],
    *,
    settings: AccessControlSettings | None = None,
) -> None:
    """Authorize ``caller_identity`` to dispatch ``operation_metadata``.

    Returns on allow; raises :class:`PermissionDeniedError` on deny. With access
    control disabled everything is allowed (matching the HTTP edge, where no
    middleware runs). The internal principal is allowed; an external caller with
    no resolvable identity is denied fail-closed.

    The HTTP edge's terms, all of them, ANDed fail-closed: route→resource resolution, the
    caller's policy (and an owned key's owner's), the scope test, the jq fences, and the
    per-tag LEVEL pass. The pre-auth login surface short-circuits ahead of all of them but
    never ahead of the route pin. The store's policy version is read ONCE and threaded
    through every versioned read, so no layer answers from a pre-bump cache slot while
    another answers from a post-bump one.

    Everything deciding the CALLER's authority is read live per decision, so a revocation
    lands on a fire's very next dispatch.
    """
    ac_settings = settings if settings is not None else access_control_settings()
    if not ac_settings.enable:
        return
    if caller_identity.is_internal:
        return
    user_id = caller_identity.user_id
    if user_id is None:
        raise PermissionDeniedError("access denied: no caller identity for an external tool dispatch")

    # A background fire rather than a request; keys several terms of the shared tail.
    is_execution_fire = get_execution_identity() is not None

    # The one target every layer of the tail keys on — its canonical form, the SAME
    # ``.request.path`` the HTTP edge's jq reads — pinned to the operation's OWN registered
    # route before ANY layer, the always-public short-circuit included, reads it. Method
    # defaults to POST for a route that declares none.
    method = operation_metadata.http_method or "POST"
    synthesized = synthesize_path(operation_metadata, call_arguments)
    try:
        path = canonicalize_path(synthesized)
    except MalformedPathError as exc:
        raise PermissionDeniedError(
            f"access denied: {method} {synthesized} is not a well-formed path for {operation_metadata.name!r}"
        ) from exc
    route = _own_route(operation_metadata, path, method)

    await _authorize_pinned_route(
        caller_identity,
        ac_settings,
        user_id=user_id,
        path=path,
        method=method,
        route=route,
        is_execution_fire=is_execution_fire,
    )


async def _authorize_pinned_route(
    caller_identity: CallerIdentity,
    ac_settings: AccessControlSettings,
    *,
    user_id: str,
    path: str,
    method: str,
    route: RouteMetadata,
    is_execution_fire: bool,
) -> None:
    """Run the post-pin authorization tail over a target already resolved to ``path``, ``method`` and ``route``.

    ``path`` is the canonical target. The ONE spelling of the HTTP edge's decision
    downstream of the route pin, shared by :func:`check` and
    :func:`~tai42_skeleton.authz.execution.authorize_execution_agent_run` so neither can
    drift onto a narrower one. ``is_execution_fire`` is the caller's to decide; it keys
    the deleted-principal refusal, the fingerprint re-assert, the live effective-scope
    derivation and the token-free-evaluable re-assert. Returns on an allow; raises
    :class:`PermissionDeniedError` on a deny, and never grants on a read fault.
    """
    # The store's policy VERSION, read ONCE and threaded through EVERY versioned read
    # below, so no layer serves a pre-bump cached copy while another serves a post-bump
    # one. A read fault denies fail-closed.
    enforcer = policy_enforcer(ac_settings)
    version = await _read_pinned_policy_version(enforcer, user_id)

    # 1. Route -> resource ids, through the edge's one memoized verifier.
    verifier = _tool_edge_verifier(ac_settings)
    resource_ids = await _resolve_pinned_resource_ids(verifier, path, method, version)

    # A pre-auth surface (the pinned route's ``pre_auth`` declaration, or an operator's
    # always-public prefix) is public regardless of the policy layers and short-circuits ahead
    # of every one of them, as it does at the HTTP edge; running them would make it HARDER to
    # reach as a tool than as its route.
    if route.pre_auth or is_always_public_prefix(path, ac_settings):
        return

    public = ac_settings.public_resource_id

    # 2. Standing + live context + scopes, pinned to ``version``.
    principal = await _resolve_principal_policies(enforcer, caller_identity, user_id, version, is_execution_fire)

    scopes = _pinned_scope_set(caller_identity, principal.standing, is_execution_fire)
    # Publicness (the public id ALONE — deny wins) relaxes the SCOPE test alone; the policy,
    # jq and LEVEL passes still run.
    if not is_public_only(resource_ids, public):
        _assert_scope_covers(resource_ids, scopes, public)

    # 3. The jq policy fences over the canonical path.
    await _enforce_pinned_conditions(enforcer, user_id, path, method, principal, scopes, is_execution_fire)

    # 4. The per-tag LEVEL pass over the pinned route.
    await _enforce_pinned_tag_level(
        principal.standing.policy, principal.standing.owner_policy, route, method, version, user_id, path
    )


@dataclass
class _PinnedPrincipal:
    """Principal resolved for a pinned tool-edge decision: its standing, the fresh live context, and its claims.

    ``claims`` are the caller's verified token claims (a fire's synthetic owner claim).
    """

    standing: Standing
    context: dict[str, Any]
    claims: dict[str, Any]


# The refusal each standing defect answers at the tool edge. A fingerprint mismatch answers
# the execution-key refusal instead (see ``_resolve_principal_policies``).
_STANDING_REFUSALS = {
    StandingDenyReason.NO_POLICY: "access denied: principal has no policy",
    StandingDenyReason.DISABLED: "access denied: principal is disabled",
    StandingDenyReason.OWNER_MISMATCH: "access denied: owner claim does not match the stored owner",
    StandingDenyReason.OWNER_DISABLED: "access denied: owner is disabled",
    StandingDenyReason.OWNER_NO_POLICY: "access denied: owner has no policy",
}


async def _read_pinned_policy_version(enforcer: PolicyEnforcer, user_id: str) -> int:
    """Read the single policy version every downstream layer is pinned to; a read fault denies fail-closed."""
    try:
        return await enforcer.current_policy_version()
    except Exception as exc:
        logger.warning("authz: policy version read failed for %s — denying", user_id, exc_info=True)
        raise PermissionDeniedError("access denied") from exc


async def _resolve_pinned_resource_ids(
    verifier: AccessControlVerifier, path: str, method: str, version: int
) -> list[str]:
    """Resolve route→resource ids through the edge's one memoized verifier.

    The ``method`` is passed so the declared-protection tier resolves the pinned op's own
    registered route (``_own_route`` already matched it for ``method``) to the universal scope
    when the operator mapped it to no row — giving the tool edge the SAME answer as the HTTP
    door instead of a fresh-deploy denial. Denies when no resource is configured (a path the
    app does not serve); a read fault denies fail-closed.
    """
    try:
        resource_ids = await verifier.resolve_resource_ids(path, method=method, policy_version=version)
    except Exception as exc:
        logger.warning("authz: route resolution failed for %s — denying", path, exc_info=True)
        raise PermissionDeniedError("access denied") from exc
    if not resource_ids:
        raise PermissionDeniedError(f"access denied: no resource configured for {method} {path}")
    return resource_ids


async def _resolve_principal_policies(
    enforcer: PolicyEnforcer,
    caller_identity: CallerIdentity,
    user_id: str,
    version: int,
    is_execution_fire: bool,
) -> _PinnedPrincipal:
    """Resolve the caller's standing and live context pinned to ``version``.

    One order for every door (:func:`~tai42_skeleton.access_control.standing.resolve_standing`):
    a principal with no policy (what a deleted key reads as) or a disabled one is denied; a
    fire's bound per-mint fingerprint is re-asserted against the LIVE policy on every dispatch,
    so a within-fire revoke+remint of the same ``user_id`` is denied rather than authorized
    against the reminted key's grants; the owner the stored policy names must be the owner
    the caller's claims carry, and must exist and be enabled. A read fault denies fail-closed.
    """
    # The caller's verified token claims; empty only on the internal/direct-construction
    # path, matching a request that carried no claims.
    claims: dict[str, Any] = dict(caller_identity.claims) if caller_identity.claims is not None else {}
    bound_fingerprint: str | None = None
    if is_execution_fire:
        bound_fingerprint = caller_identity.execution_key_fingerprint
        if bound_fingerprint is None:
            # An invariant breach: a gate-on execution identity always carries one — ""
            # for a fingerprint-less ACCOUNT principal (resolved by the ONE equality),
            # None never. Refuse loudly rather than dispatch with no anchor at all.
            raise PermissionDeniedError("access denied: bound execution identity carries no key fingerprint")
    try:
        standing = await resolve_standing(
            enforcer, user_id, version=version, verified_claims=claims, bound_fingerprint=bound_fingerprint
        )
        context = await enforcer.get_live_context(user_id)
    except StandingDenied as denied:
        if denied.reason is StandingDenyReason.FINGERPRINT_MISMATCH:
            # Imported at call time: the execution module imports this one.
            from tai42_skeleton.authz.execution import execution_key_refusal

            raise execution_key_refusal(user_id, denied) from denied
        raise PermissionDeniedError(_STANDING_REFUSALS[denied.reason]) from denied
    except Exception as exc:
        logger.warning("authz: policy/context fetch failed for %s — denying", user_id, exc_info=True)
        raise PermissionDeniedError("access denied") from exc
    return _PinnedPrincipal(standing=standing, context=context, claims=claims)


def _pinned_scope_set(caller_identity: CallerIdentity, standing: Standing, is_execution_fire: bool) -> list[str]:
    """Return the scope set for the decision.

    On the request path this CONSUMES the auth backend's already-decided effective scopes
    (owner-attenuated), never re-deriving the attenuation; it falls back to the caller's own
    policy scopes only when none was carried. A background fire carries no attenuation
    decision, so the set is the one its standing derived from the policies just read live —
    narrowing a running key's scopes denies its very next dispatch.
    """
    if is_execution_fire:
        return standing.effective_scopes
    if caller_identity.effective_scopes is not None:
        return list(caller_identity.effective_scopes)
    return standing.policy.scopes


def _assert_scope_covers(resource_ids: list[str], scopes: list[str], public: str) -> None:
    """Assert the caller holds EVERY protected resource id, or the universal scope.

    The public id carries no scope requirement. Skipped by the caller when the id set is
    public-alone.
    """
    if not scopes_cover(resource_ids, scopes, public):
        raise PermissionDeniedError("access denied: insufficient scope")


async def _enforce_pinned_conditions(
    enforcer: PolicyEnforcer,
    user_id: str,
    path: str,
    method: str,
    principal: _PinnedPrincipal,
    scopes: list[str],
    is_execution_fire: bool,
) -> None:
    """Enforce the jq policy fences over the canonical path, keyed on {"method", "path"}.

    The passes are the backend's (:func:`~tai42_skeleton.access_control.standing.jq_passes`):
    the key's condition first, then — for an owned key whose owner carries one — the
    owner's as a SEPARATE pass over the OWNER's policy_data + scopes. Two sequential
    enforce calls are semantically AND; never concatenate the jq strings.

    A fire presents no token, so its ``.identity`` carries only the stored owner claim; each
    rendered condition is re-asserted token-free-evaluable before being enforced. An ordinary
    request carries full claims and skips this.
    """
    passes = jq_passes(
        principal.standing,
        user_id=user_id,
        claims=principal.claims,
        live_context=principal.context,
        scopes=scopes,
        now=time.time(),
    )
    try:
        for jq_pass in passes:
            rendered = await _render_condition(jq_pass.condition, principal=jq_pass.principal)
            if is_execution_fire and rendered.text and jq_pass.condition is not None:
                _assert_execution_condition_evaluable(
                    rendered.text, principal=jq_pass.principal, template_id=jq_pass.condition.id
                )
            await enforcer.enforce(jq_pass.context_for(method, path), rendered)
    except AuthenticationError as exc:
        raise PermissionDeniedError("access denied: policy condition rejected") from exc
    except PermissionDeniedError:
        # The render and token-free-evaluable refusals are already final decisions; re-raise
        # so the catch-all below cannot flatten them into a generic denial.
        raise
    except Exception as exc:
        logger.warning("authz: policy enforcement failed for %s — denying", user_id, exc_info=True)
        raise PermissionDeniedError("access denied") from exc


async def _enforce_pinned_tag_level(
    policy: AccessPolicy,
    owner_policy: AccessPolicy | None,
    route: RouteMetadata,
    method: str,
    version: int,
    user_id: str,
    path: str,
) -> None:
    """Run the per-tag LEVEL pass over the pinned route, never re-resolved from the caller-influenced path.

    The policies are already read and keyed on the SAME version, so the grant cache answers
    from their generation. It fences a fenced/secret operation to an admin. An infra fault
    fails closed.
    """
    try:
        allowed, cause = await role_level_decision_for_route(policy, owner_policy, route, method, version)
    except Exception as exc:
        logger.warning("authz: per-tag level resolution failed for %s — denying", user_id, exc_info=True)
        raise PermissionDeniedError("access denied") from exc

    if not allowed:
        logger.warning(
            "authz: per-tag level denied %s on %s %s (%s)",
            user_id,
            method,
            path,
            cause.value if cause is not None else "deny",
        )
        raise PermissionDeniedError(f"access denied: {method} {path} is not permitted for {user_id!r}")
