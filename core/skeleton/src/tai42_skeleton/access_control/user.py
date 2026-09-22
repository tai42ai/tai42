"""The authenticated principal and the caller-identity/isolation helpers the request scope shares."""

from collections.abc import Mapping
from typing import Any

from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from tai42_contract.access_control import OWNER_USER_ID_CLAIM, get_current_user_id
from tai42_contract.access_control.models import AccessPolicy

from tai42_skeleton.access_control.request_scopes import get_request_identity_claims, get_request_is_admin


def effective_scopes(key_scopes: list[str], owner_scopes: list[str]) -> list[str]:
    """The scopes an owned key actually carries: its own scopes ∩ the owner's CURRENT scopes.

    ``"*"`` behaves as "everything" on BOTH sides (``"*" ∩ X = X``). Three explicit cases: a ``"*"`` owner
    caps nothing (the key keeps its scopes); a ``"*"`` KEY under a scoped owner collapses to the owner's
    scopes (a plain membership filter would wrongly yield ``[]`` here); otherwise a plain intersection
    preserving the key's order.
    """
    if "*" in owner_scopes:
        return list(key_scopes)
    if "*" in key_scopes:
        return list(owner_scopes)
    owner_set = set(owner_scopes)
    return [scope for scope in key_scopes if scope in owner_set]


def is_admin_policy(policy: AccessPolicy, owner_policy: AccessPolicy | None) -> bool:
    """Whether the caller is ADMIN, computed on its EFFECTIVE (owner-attenuated) policy.

    This is the single spelling of "admin" every consumer shares (the key-management
    ownership rules, the capability projection, the fence exemption).

    Admin iff the EFFECTIVE scopes grant ``"*"`` AND the key's own condition is ``None``
    AND the owner's condition (when there is an owner) is ``None``. Attenuation combines
    only SCOPES, so a condition on EITHER side is enforced separately and must be absent
    for admin. Role-holders carry ``["*"]`` scopes plus a jq condition, so a scopes-only
    test would classify every editor/viewer as admin; and an editor-minted condition-free
    ``["*"]`` owned key is denied admin because it inherits its owner's jq base through the
    owner's condition (the you-plus escalation this conjunct closes). The owner's OWN key
    is admin because the owner is: both conditions are ``None`` and the effective scopes are
    ``"*"``. ``owner_policy`` is the owner's CURRENT stored policy (``None`` for a top-level
    principal), so the classification is byte-identical wherever it is used.
    """
    owner_scopes = owner_policy.scopes if owner_policy is not None else ["*"]
    return (
        "*" in effective_scopes(policy.scopes, owner_scopes)
        and policy.condition is None
        and (owner_policy is None or owner_policy.condition is None)
    )


class TaiUser(AuthenticatedUser):
    """The authenticated principal placed in the request scope on a fully successful auth + policy pass.

    Subclassing the mcp-SDK ``AuthenticatedUser`` is what makes the SDK's
    bearer-auth route gate (``RequireAuthMiddleware``, which admits a request only
    when ``isinstance(scope["user"], AuthenticatedUser)``) and its
    ``AuthContextMiddleware`` (which powers ``get_access_token()`` inside tools)
    recognize this authenticated principal on the main ``/mcp`` and ``/sse``
    endpoints and admit the request.

    ``fastmcp``'s ``AccessToken`` subclasses the mcp-SDK ``AccessToken`` that
    ``AuthenticatedUser.__init__`` expects, so the token the backend already holds
    is passed straight through. ``.token`` is retained for
    ``ResourceGuardMiddleware`` (which reads ``user.token.client_id``).
    """

    def __init__(self, token: AccessToken, is_admin: bool = False):
        """Wrap the resolved ``token`` and record whether the backend classified it as admin."""
        super().__init__(token)
        self.token = token
        # Whether this principal is the ADMIN discriminator (a condition-free ``"*"``
        # policy that is not an owned key — see :func:`is_admin_policy`). Computed
        # server-side by the auth backend from the resolved policy and stamped here, so
        # it cannot be forged by a provider claim. The resource guard reads it to admit a
        # super-admin to a route with no configured row: a root identity is never gated
        # by a missing route mapping (it can map the route anyway), so blocking it is a
        # footgun, not security — while every non-admin identity still fails closed.
        self.is_admin = is_admin

    @property
    def identity(self) -> str:
        """The caller's identity — the token's ``client_id``."""
        return self.token.client_id


def _acting_principal() -> tuple[str | None, Mapping[str, Any] | None, bool]:
    """``(own id, claims, is_admin)`` of the principal ACTING at the current dispatch.

    A bound execution identity takes PRECEDENCE, never a fallback: a fire dispatched as a
    Starlette ``BackgroundTask`` runs inside the triggering request's contextvar context,
    so the request-scope vars are still that caller's and must not be consulted while a
    fire is bound. Under a fire all three facts come from the bound identity; outside one
    they are the request-scope caller's. ``own``/``claims`` are ``None`` and ``is_admin``
    ``False`` when none is bound.

    ``is_admin`` is the auth backend's admin verdict, computed once when it bound the
    caller: it rides on the execution identity (built at fire-open) and, in the request
    scope, is the admin fact the guard middleware stamps alongside the claims — read here,
    never re-derived from policies.

    The same rule :func:`~tai42_skeleton.operations._authority.resolve_caller` applies, so
    isolation and the pass-role gate never key on different principals.

    Imported at call time: ``authz`` reaches this module back through
    ``access_control.backend``.
    """
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    identity = get_execution_identity()
    if identity is not None:
        return identity.user_id, identity.claims, identity.is_admin
    return get_current_user_id(), get_request_identity_claims(), get_request_is_admin()


def restricted_identity() -> str | None:
    """The identity a RESTRICTED caller is isolated to — its OWN id — or ``None`` when unrestricted.

    An ADMIN caller is NEVER restricted: under identity-first every api key carries an owner
    claim, so the owner's own ADMIN key would otherwise be confined to its slice. Admin is
    the enforcement's own verdict (:func:`is_admin_policy` on the owner-attenuated effective
    policy), read off :func:`_acting_principal` — so it holds for both a request-scope caller
    and a fire-bound execution identity, and it is never re-derived from policies here.

    A NON-admin caller is restricted iff its claims carry ``OWNER_USER_ID_CLAIM`` — a
    non-admin owned key acting on behalf of its owner. Being an owned key is what CONFINES
    it, but the identity it is confined to is its OWN id (its ``user_id`` / token
    ``client_id``), NOT its owner's: each owned key is its own island. A restricted caller
    sees and touches ONLY the tool runs, interactions, and notifications belonging to
    (addressed to) its OWN key identity — never its owner's, never a sibling owned key's.

    ``None`` therefore covers the admin caller, a session / top-level principal (no owner
    claim), and the unauthenticated / gate-off cases where no caller is bound. This helper
    is the one definition of "restricted" the whole codebase shares.

    The deciding facts come from :func:`_acting_principal`, so a fire is isolated to the key
    it is authorized as rather than to whoever triggered it, and no Starlette ``Request`` is
    needed — the flat-argument operation doors can enforce isolation without one. With the
    gate off no claims are bound, so the result is ``None``.
    """
    own, claims, is_admin = _acting_principal()
    if is_admin:
        return None
    if claims is None or claims.get(OWNER_USER_ID_CLAIM) is None:
        return None
    if own is None:
        # An owner-claim-bearing principal always has a bound own id. A miss is a broken
        # invariant, not a state to isolate to nothing: confining to None would open the
        # full view to a restricted caller.
        raise RuntimeError("owner-claim-bearing caller has no bound own id; identity invariant broken")
    return own


class CrossIdentityAudienceError(Exception):
    """A RESTRICTED caller tried to address an ``audience`` other than its own identity.

    The cross-identity inject/exfil attempt :func:`clamp_write_audience` rejects.

    It is an AUTHORIZATION denial, NOT input validation: a write door (``notify_user``)
    maps it to the same ``403``/``ForbiddenError`` the read-side answer door raises for
    the symmetric cross-identity read denial, so both boundary violations surface as
    403 — distinct from the blank-audience ``ValueError`` a door validates as a 400.
    Kept as an access-control domain exception (not the operations-layer
    ``ForbiddenError``) so this foundational module stays free of an upward operations
    dependency; the door owns the mapping.
    """


def clamp_write_audience(audience: str | None) -> str | None:
    """Scope the ``audience`` a write door may address to the caller's own slice.

    The WRITE-side dual of the isolation read clamps, for a write door (``ask`` /
    ``notify_user``).

    A RESTRICTED caller (:func:`restricted_identity` returns a non-None id — an owned
    key confined to its OWN slice) may address ONLY its own identity, so its writes
    land exclusively in its own isolation slice — the write-side guarantee the read
    clamps assume. ``audience is None`` is scoped to SELF (the owned key addresses its
    own slice), ``audience == own id`` passes unchanged, and ANY OTHER identity is a
    loud :class:`CrossIdentityAudienceError` (a cross-identity inject/exfil attempt
    through another identity's slice) — an AUTHORIZATION denial the write doors map to
    a ``403``, mirroring the read-side answer door, NOT the blank-audience
    ``ValueError``/400. An UNRESTRICTED caller (admin / system / a top-level principal's
    execution key / no bound principal at all) is returned unchanged — it may address any
    identity, or broadcast with ``audience is None``.

    Returns the audience the door must persist. A door runs its own blank-audience
    validation first; this clamp is in addition to it.
    """
    own = restricted_identity()
    if own is None:
        return audience
    if audience is None:
        return own
    if audience != own:
        raise CrossIdentityAudienceError("a restricted caller may address only its own identity")
    return audience


def request_identity() -> tuple[str | None, str | None]:
    """``(user_id, restricted)`` for the current caller, resolved once so a door never re-derives it.

    ``user_id`` is the acting principal's own id (:func:`_acting_principal`), so a fire's
    writes are attributed to the KEY rather than to whoever triggered it. The second
    element is the ISOLATION identity (:func:`restricted_identity`) — the caller's OWN id
    when restricted, else ``None`` (unrestricted → full view). ``restricted is not None``
    is the restricted test. Both are ``None`` when no principal is bound; a gate-off FIRE
    still binds its key, so only the isolation half is ``None`` there. When restricted,
    the two are the SAME id.
    """
    return _acting_principal()[0], restricted_identity()
