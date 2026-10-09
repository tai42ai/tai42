"""Roles: the versioned store view, the seeded defaults, the LIVE apply helper, and the admin services impl.

A role is an operator-authored, versioned permission set under two layers. Layer 1 is a
KEPT base-tier jq security ceiling carried on ``condition`` (owner-scoping, the
``/api/auth`` control-plane gate, the viewer read-only ceiling); Layer 2 is the editable
per-tag ACCESS LEVEL map ``grants`` (feature-group tag → ``none``/``read``/``write``).
Enforcement INTERSECTS the two with the route action-class fence, fail-closed.

Roles are LIVE: a user's enforced policy carries a role-name POINTER
(``policy_data[ROLE_POINTER_KEY]``, never the policy condition), and enforcement resolves
the role's CURRENT grant map at request time — an edit to a role changes every holder's
reach on their next request. Roles are stored under the generic
:class:`~tai42_contract.versioning.VersionedStore` as ``kind="role"`` — a view mirroring
:class:`~tai42_skeleton.access_control.policy_store.AcPolicyStore` and
:class:`~tai42_skeleton.presets.store.PresetStoreView`.

**The seeded roles carry ``"*"`` scopes and differ by their base-tier jq + grant map:**
``"*"`` is the universal scope — it covers every registered route, including one the
operator mapped to no row (the verifier's declared-protection tier resolves such a route
to ``"*"``), so a role-holder reaches the surface on a fresh deployment while a scoped key
still needs an operator row. The roles therefore need no route-table surgery and work on
any deployment; a seeded scope re-mapping of the route table would instead break every
existing scoped key. The base-tier jq carries ``.request.method``/``.request.path``.

- ``admin``: unconditional ``["*"]``, ``allow_all`` — full control including
  access-control administration. Its per-tag pass is SKIPPED at enforcement; it is
  reserved, permanent, and un-lockable.
- ``editor``: ``["*"]`` under :func:`editor_jq` (the ``/api/auth`` control-plane gate with
  self-service carve-ins), with ``write`` on every grantable feature tag — everything
  EXCEPT the admin-only fence and the access-control admin area, every route that declares
  ``self_service=True`` carved back in on the methods it serves.
- ``viewer``: ``["*"]`` under :func:`viewer_jq` (the viewer read-only ceiling), with ``read``
  on every grantable feature tag — read-only plus the state-changing self-service routes.

The base-tier jq is BUILT per deployment from the registered routes' ``self_service``
declarations (core and plugin alike), so the platform names no route in it. Seeding is
create-only: a store seeded earlier keeps its text, which grants the same reach.

No ``/api/login`` clause exists in either base-tier string: a public route resolves to the
public resource id before any jq evaluates, so a login-namespace carve-out would be dead text.

A self-service route that is ``secret``/``fenced`` (the policy-administration routes
``/api/auth/api-keys/{user_id}/policy/versions`` and ``.../policy/rollback``) is still
enforced ADMIN-ONLY by its action class: a non-admin editor/viewer is denied there
regardless of this jq, so it can never read another user's policy history nor roll an
enforced policy back to a prior version.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.access_control.models import AccessPolicy, RoleDefinition
from tai42_contract.template import TemplatedText
from tai42_contract.versioning import VersionedStore, VersionedStoreTransaction
from tai42_contract.versioning.errors import DocumentExistsError, DocumentNotFoundError
from tai42_contract.versioning.models import DocumentRecord, DocumentVersion

from tai42_skeleton.access_control import management
from tai42_skeleton.access_control.policy_store import ac_policy_store
from tai42_skeleton.access_control.store import access_control_store
from tai42_skeleton.access_control.user import is_admin_policy

_KIND = "role"

# The reserved, permanent role name: undeletable, unrenamable, non-downgradable — its jq
# base stays ``None`` and it is ``allow_all`` (the per-tag pass skipped), so an admin can
# never be locked out of the control plane from either direction.
RESERVED_ADMIN_ROLE = "admin"

# The policy_data key holding a user's LIVE role pointer — the role NAME whose CURRENT
# grant map governs the user, resolved per request. It is orthogonal metadata read ONLY
# for the grant lookup: it is NEVER routed through the policy condition (which would
# collide with the ``is_admin_policy`` discriminator), and an ``allow_all``/admin policy carries
# no pointer so that discriminator holds byte-for-byte.
ROLE_POINTER_KEY = "role"

# The platform-owned control-plane ceiling clause: everything OUTSIDE ``/api/auth`` (the
# control-plane boundary the seed fences, not a route). Every caller self-service surface
# under it is carved back in from its route's ``self_service`` declaration (see
# :func:`_self_service_route_clauses`), so the platform names no route here — its own or a
# provider's. The admin-only mutation fence is the route action-class (enforced in code), not
# composed here.
_EDITOR_FIXED_CARVE = '((.request.path | startswith("/api/auth")) | not)'

# The methods a viewer's read-only ceiling admits on any surface.
_READ_METHODS = ("GET", "HEAD", "OPTIONS")


def self_service_routes() -> list[tuple[str, frozenset[str]]]:
    """Every registered route that DECLARES itself a caller self-service surface, with its served methods.

    Sorted ``(canonical path, served methods)`` pairs for every route registered with
    ``self_service=True``, core or plugin, derived from the live route registry exactly as
    :func:`grantable_feature_tags` derives the grant map. A templated route keeps its
    template; the carve-in matches it as one.
    """
    from tai42_skeleton.access_control.path_canon import canonicalize_path
    from tai42_skeleton.access_control.role_gate import served_methods
    from tai42_skeleton.app.route_registry import load_all_routes

    routes = {(canonicalize_path(meta.path), served_methods(meta)) for meta in load_all_routes() if meta.self_service}
    return sorted(routes, key=lambda route: (route[0], sorted(route[1])))


def _self_service_clauses() -> list[str]:
    """One jq clause per distinct served-method set, admitting every self-service route serving it.

    Routes serving the same methods share one clause: those methods, and the request path
    matching one anchored alternation of the routes' paths (a concrete path literally, a
    templated one by its template). A string literal is one token whatever its length, so the
    ceiling's size grows with the number of distinct method sets, not of routes, and stays
    inside the token-free scan's allowance an execution-key bind runs over the role condition.
    """
    from tai42_skeleton.access_control.role_gate import route_template_regex

    groups: dict[frozenset[str], list[str]] = {}
    for path, methods in self_service_routes():
        body = route_template_regex(path).pattern[1:-1] if "{" in path else re.escape(path)
        groups.setdefault(methods, []).append(body)
    clauses = []
    for methods in sorted(groups, key=sorted):
        method_set = ",".join(json.dumps(method) for method in sorted(methods))
        alternation = json.dumps(f"^(?:{'|'.join(sorted(groups[methods]))})$")
        clauses.append(f"((.request.method | IN({method_set})) and (.request.path | test({alternation})))")
    return clauses


def _self_service_route_clauses() -> str:
    """The jq ``or``-clauses carving every declared self-service route into the ceiling.

    Each admits a route's served methods on its path (a templated route matched by its
    template). Returns ``""`` when no route declares itself self-service, leaving the ceiling
    to the platform-owned clause alone.
    """
    return "".join(f" or {clause}" for clause in _self_service_clauses())


def _viewer_write_clauses() -> str:
    """The self-service clauses whose methods include a state-changing one — a viewer's only writes.

    ``"false"`` when no self-service route serves one.
    """
    clauses = [
        clause
        for clause, methods in zip(_self_service_clauses(), _method_sets(), strict=True)
        if any(method not in _READ_METHODS for method in methods)
    ]
    return " or ".join(clauses) if clauses else "false"


def _method_sets() -> list[frozenset[str]]:
    """The distinct served-method sets of the self-service routes, in :func:`_self_service_clauses` order."""
    return sorted({methods for _path, methods in self_service_routes()}, key=sorted)


def editor_jq() -> str:
    """The editor base-tier jq ceiling: everything outside ``/api/auth`` plus every declared self-service route."""
    return f"({_EDITOR_FIXED_CARVE}{_self_service_route_clauses()})"


def viewer_jq() -> str:
    """The viewer base-tier jq ceiling.

    Read-only methods OR a declared self-service route serving a state-changing method
    (first conjunct), intersected with the editor control-plane ceiling (second conjunct), so
    a viewer's state-changing calls are confined to the self-service surfaces. The admin-only
    mutation fence is the route action-class (enforced in code), not composed here.
    """
    read_methods = f"(.request.method | IN({','.join(json.dumps(m) for m in _READ_METHODS)}))"
    return (
        f"((({_viewer_write_clauses()}) or {read_methods}) and ({_EDITOR_FIXED_CARVE}{_self_service_route_clauses()}))"
    )


def grantable_feature_tags() -> set[str]:
    """Every feature-group tag carrying at least one GRANTABLE gated route — the tags a level can open.

    A GRANTABLE route is ``read``/``write`` and non-fenced. A tag whose routes are ALL
    fenced/secret is admin-only and never appears here (a level can never open it).
    """
    from tai42_skeleton.app.route_registry import load_all_routes

    tags: set[str] = set()
    for meta in load_all_routes():
        if meta.authed and meta.action in ("read", "write"):
            tags.update(meta.tags)
    return tags


def _seeded_roles() -> list[dict[str, Any]]:
    """The default role bodies (``RoleDefinition`` dumps).

    ``editor``/``viewer`` derive their grant maps from the live registry so a new
    grantable feature area joins the default reach automatically; the bulk-secret
    reads are ``action=secret`` (fenced) so they never appear in any grant map.
    """
    grantable = sorted(grantable_feature_tags())
    return [
        RoleDefinition(
            name="admin",
            description="Full control, including access-control management.",
            allow_all=True,
            grants={},
            condition=None,
        ).model_dump(),
        RoleDefinition(
            name="editor",
            description="Everything except access-control administration; may manage own API keys.",
            base_tier="editor",
            grants=dict.fromkeys(grantable, "write"),
            condition=TemplatedText(content=editor_jq()),
        ).model_dump(),
        RoleDefinition(
            name="viewer",
            description="Read-only, plus login/logout and own-key management.",
            base_tier="viewer",
            grants=dict.fromkeys(grantable, "read"),
            condition=TemplatedText(content=viewer_jq()),
        ).model_dump(),
    ]


class RoleStoreView:
    """Typed role view delegating to a generic :class:`VersionedStore` under ``kind="role"``.

    The body is a :class:`RoleDefinition` dump.
    """

    def __init__(self, store: VersionedStore) -> None:
        """Wrap the generic versioned ``store`` this view reads and writes roles through."""
        self._store = store

    async def seed(self, name: str, body: dict[str, Any]) -> bool:
        """Create the role only if it does not exist (idempotent create-only).

        Returns ``True`` when a new role was created, ``False`` when one already existed
        and was left untouched (an operator edit survives a re-seed).

        Concurrent-boot safe: replicas seeding the same role at once converge on ONE
        active row — the store's create ABSORBS the live-duplicate conflict rather than
        raising a unique violation, so the losing replica takes the ``False`` branch
        quietly, with no spurious duplicate-key ERROR in the server log.
        """
        try:
            await self._store.create(_KIND, name, body)
        except DocumentExistsError:
            return False
        else:
            return True

    async def create(self, name: str, body: dict[str, Any], tx: VersionedStoreTransaction | None = None) -> None:
        """Create a brand-new role.

        Raises ``DocumentExistsError`` on a name collision (the caller maps it to a loud
        409). Runs within ``tx`` when one is supplied.
        """
        await self._store.create(_KIND, name, body, tx=tx)

    async def update(self, name: str, body: dict[str, Any], tx: VersionedStoreTransaction | None = None) -> None:
        """Persist an edit as a NEW version (versioned history + rollback come free).

        Raises ``DocumentNotFoundError`` when the role does not exist (loud 404). Runs
        within ``tx`` when one is supplied.
        """
        await self._store.save_version(_KIND, name, body, tx=tx)

    async def delete(self, name: str, tx: VersionedStoreTransaction | None = None) -> None:
        """Hard-delete the active role and its version rows.

        Raises ``DocumentNotFoundError`` when the role does not exist. Runs within ``tx``
        when one is supplied.
        """
        await self._store.delete(_KIND, name, tx=tx)

    async def rename(self, name: str, new_name: str) -> DocumentRecord:
        """Re-key the role, moving its whole history untouched."""
        return await self._store.rename(_KIND, name, new_name)

    async def list_versions(self, name: str) -> list[DocumentVersion]:
        """The version history of role ``name``."""
        return await self._store.list_versions(_KIND, name)

    async def get_version(
        self, name: str, version: int, tx: VersionedStoreTransaction | None = None
    ) -> DocumentVersion:
        """The immutable body of one version.

        Runs within ``tx`` when one is supplied, so a rollback's before/after reads ride
        the transaction's connection.
        """
        return await self._store.get_version(_KIND, name, version, tx=tx)

    async def rollback(self, name: str, version: int, tx: VersionedStoreTransaction | None = None) -> DocumentRecord:
        """Restore an earlier ``version`` as a new active version. Runs within ``tx`` when supplied."""
        return await self._store.rollback(_KIND, name, version, tx=tx)

    async def get_active_body(
        self, name: str, *, tx: VersionedStoreTransaction | None = None, for_update: bool = False
    ) -> dict[str, Any]:
        """The active body of role ``name``.

        Raises ``DocumentNotFoundError`` when the role does not exist. Passed a ``tx`` the
        read rides that transaction's connection (no second pooled connection while the
        transaction is open); with ``for_update=True`` it row-locks the active role so a
        read-modify-write serializes against a concurrent edit — the lock needs the
        transaction, so ``for_update`` requires ``tx``.
        """
        return await self._store.get_active_body(_KIND, name, tx=tx, for_update=for_update)

    async def list_roles(self) -> list[dict[str, Any]]:
        """Every role's active body as a full ``RoleDefinition``-shaped dict.

        Each dict is ``{name, description, scopes, condition, base_tier, allow_all,
        grants}`` — the listing shape the roles route returns.
        """
        records = await self._store.list(_KIND)
        roles: list[dict[str, Any]] = []
        for record in records:
            body = await self._store.get_active_body(_KIND, record.name)
            body = {**body, "name": record.name}
            roles.append(body)
        return roles


def role_store() -> RoleStoreView:
    """Build the active role view over the generic versioned store."""
    from tai42_skeleton.versioning import versioned_store

    return RoleStoreView(versioned_store())


@asynccontextmanager
async def role_write_transaction() -> AsyncIterator[VersionedStoreTransaction]:
    """One versioned-store transaction for a role-definition write; the policy version is bumped after commit.

    A raise inside the body rolls the transaction back and propagates before the bump, so a
    rolled-back write invalidates nothing. The bump makes every holder's next request read
    the role's committed grants.
    """
    from tai42_skeleton.versioning import versioned_store

    async with versioned_store().transaction() as tx:
        yield tx
    await management.bump_policy_version()


async def seed_default_roles() -> None:
    """Seed the default admin/editor/viewer roles, idempotent create-only.

    An operator-edited role is never overwritten by a re-seed.
    """
    store = role_store()
    for body in _seeded_roles():
        await store.seed(body["name"], body)


async def apply_role(user_id: str, role_name: str) -> None:
    """Assign role ``role_name`` to ``user_id``'s ENFORCED policy (LIVE semantics), last-admin guarded.

    Writes the ``scopes`` + the role's ``condition`` (overwritten whole, so a
    re-assignment never strands a prior role's condition) AND the role-name POINTER
    merged into the user's existing ``policy_data`` — preserving the disabled marker and key
    ownership. It does NOT freeze a grant-map COPY: enforcement resolves the role's
    CURRENT grants through the pointer, so a later role edit retro-applies.

    An ``allow_all``/admin role carries NO pointer (its per-tag pass is skipped, and the
    absent pointer keeps the ``is_admin_policy`` discriminator intact) — a stale pointer
    from a prior non-admin role is dropped on the admin assignment.

    Runs in one advisory-locked guard transaction: the policy row is read ``FOR UPDATE``,
    a non-``allow_all`` role on the last enabled admin principal raises
    :class:`~tai42_contract.accounts.errors.LastAdminError` with nothing written, and the
    row is written (created when absent, so the bootstrap owner and every admin-created or
    invited user never stay on the empty ``AccessPolicy()`` default). Enforcement is
    refreshed after the commit. Raises ``KeyError`` on an unknown role (loud), and
    ``ValueError`` when ``user_id`` is a minted api key's id (a role belongs to a principal).
    """
    try:
        body = await role_store().get_active_body(role_name)
    except DocumentNotFoundError as exc:
        raise KeyError(f"unknown role: {role_name!r}") from exc

    role = RoleDefinition(**body)

    # Fail-closed guard on the admin discriminator: a non-allow_all role MUST carry a
    # base-tier condition. A role carrying ``condition=None`` would assign a
    # condition-free ["*"] policy that ``is_admin_policy`` reads as FULL ADMIN — the role
    # pointer is orthogonal metadata the discriminator ignores. Only the reserved
    # allow_all admin role is legitimately condition-free; refuse to mint an admin-shaped
    # policy from any other role rather than silently escalate it.
    if not role.allow_all and role.condition is None:
        raise ValueError(
            f"role {role_name!r} is not allow_all yet carries no base-tier condition; assigning it would "
            "produce a condition-free ['*'] policy the admin discriminator misreads as full admin — refusing"
        )

    async with access_control_store().principal_guard_txn() as guard:
        existing = await guard.policy_body(user_id)
        if existing is not None and management.is_minted_policy(existing):
            # A key's own row is a credential's policy, never a principal's: assigning a role to
            # it would leave the key's fingerprint and owner on a "principal" enforced as that
            # owner's key.
            raise ValueError(
                f"user id {user_id!r} is an api key's id; a role is assigned to a principal, never to a key"
            )
        policy_data = dict((existing or {}).get("policy_data") or {})
        if role.allow_all:
            policy_data.pop(ROLE_POINTER_KEY, None)
        else:
            policy_data[ROLE_POINTER_KEY] = role_name
        committed = {
            "scopes": list(role.scopes),
            "policy_data": policy_data,
            "condition": role.condition.model_dump() if role.condition is not None else None,
        }
        await guard.refuse_if_last_admin(user_id, committed)
        await guard.write_policy(user_id, committed)

    await refresh_enforcement(user_id, committed)


async def principal_roles(user_ids: Sequence[str]) -> dict[str, str | None]:
    """The role each principal holds, read in one query.

    :data:`RESERVED_ADMIN_ROLE` when its own policy is admin-shaped, else its role pointer,
    else ``None`` (a policy not written from a role template). A user id with no principal
    is absent from the result.
    """
    policies = await access_control_store().principal_policies(user_ids)
    result: dict[str, str | None] = {}
    for user_id, body in policies.items():
        if is_admin_policy(AccessPolicy(**body), None):
            result[user_id] = RESERVED_ADMIN_ROLE
            continue
        pointer = body["policy_data"].get(ROLE_POINTER_KEY)
        result[user_id] = pointer if isinstance(pointer, str) and pointer else None
    return result


async def create_principal(
    user_id: str,
    *,
    kind: str,
    display_name: str,
    created_by: str | None,
    role: str,
) -> dict[str, Any]:
    """Create a principal row and apply its role, with compensation on role failure.

    Writes the principal row FIRST (the store rejects a duplicate ``user_id`` with a loud
    ``ValueError``), then applies the role template onto the principal's enforced policy.
    If :func:`apply_role` fails (an unknown role, a store fault) the principal row is
    deleted and the error re-raised, so a failed create never strands a role-less
    principal. Returns the created principal row.
    """
    store = access_control_store()
    principal = await store.create_principal(user_id, kind, display_name, created_by)
    try:
        await apply_role(user_id, role)
    except Exception:
        # Compensation: the role never landed, so the principal row must not survive.
        await store.delete_principal(user_id)
        raise
    return principal


async def refresh_enforcement(user_id: str, committed: dict[str, Any]) -> None:
    """Bump the policy version and write the committed body into the enforcement cache.

    The post-commit half of a guarded principal change (a role assignment or a disabled
    flip): once the advisory-locked transaction has committed, enforcement is refreshed so
    the change takes effect on the next request.
    """
    await management.bump_policy_version()
    await ac_policy_store().write(user_id, committed)


async def revoke_owned_keys_and_bump(user_id: str) -> None:
    """Revoke every api key ``user_id`` owns, then bump the policy version.

    The post-commit half of a principal delete, shared by the guarded principals door and the
    accounts-services wrapper. Owned keys are walked from the management/listing home
    (``policy_data``'s ``OWNER_USER_ID_CLAIM``); each revoke touches the identity provider
    (Redis) and so cannot join the guarded delete transaction. Running it AFTER that
    transaction commits keeps the last-admin count and the row delete atomic (a refusal rolls
    back with nothing revoked): the owned-key policy rows carry their own fingerprint and
    owner claim, so the listing still finds them once the principal's own unfingerprinted row
    is gone, and a revoke of an already-orphaned key is idempotent.
    """
    for entry in await management.get_all_existing_tokens_payload():
        owner = (entry.get("policy_data") or {}).get(OWNER_USER_ID_CLAIM)
        if owner == user_id:
            await management.revoke_api_key(entry["user_id"])
    await management.bump_policy_version()


async def set_principal_disabled(user_id: str, disabled: bool) -> None:
    """Flip a principal's disabled state under the advisory-locked guard, then refresh enforcement.

    A disable of the last enabled admin principal raises
    :class:`~tai42_contract.accounts.errors.LastAdminError` with nothing written; a
    re-enable needs no count. The guard writes both the authoritative
    ``access_control_principals.disabled`` column and its ``policy_data['disabled']``
    projection in one transaction; enforcement is refreshed after it commits. Raises
    ``KeyError`` when the principal (or its policy row) is absent.
    """
    async with access_control_store().principal_guard_txn() as guard:
        if disabled:
            await guard.refuse_if_last_admin(user_id)
        committed = await guard.set_disabled(user_id, disabled)
    await refresh_enforcement(user_id, committed)


async def delete_principal(user_id: str) -> None:
    """Delete a principal's policy row and principal row under the advisory-locked guard, then revoke owned keys.

    Deleting the last enabled admin principal raises
    :class:`~tai42_contract.accounts.errors.LastAdminError` with nothing written. The guard
    deletes both rows in one transaction; owned keys are revoked AFTER it commits (see
    :func:`revoke_owned_keys_and_bump`). The principal is expected to EXIST: a missing
    policy row or principal row is an invariant breach and raises ``KeyError`` (loud)
    rather than proceeding silently.
    """
    async with access_control_store().principal_guard_txn() as guard:
        await guard.refuse_if_last_admin(user_id)
        policy_existed, principal_existed = await guard.delete(user_id)
        if not policy_existed:
            raise KeyError(f"cannot remove policy for unknown principal: {user_id!r}")
        if not principal_existed:
            raise KeyError(f"cannot remove unknown principal: {user_id!r}")
    await revoke_owned_keys_and_bump(user_id)


class SkeletonAccountsAdminServices:
    """The application's implementation of the ``AccountsAdminServices`` Protocol.

    Injected onto ``settings.admin`` at ``AuthAdapter`` construction so every
    accounts-provider factory reaches it as ``settings.admin`` (never by importing this
    module). Every method mutates application-owned policy state and bumps the policy
    version so enforcement follows immediately; the role, disable and remove methods hold
    the last-admin guard.
    """

    async def create_principal(
        self,
        user_id: str,
        *,
        kind: str,
        display_name: str,
        created_by: str | None,
        role: str,
    ) -> None:
        """Create the principal row and apply its role; see :func:`create_principal`."""
        await create_principal(user_id, kind=kind, display_name=display_name, created_by=created_by, role=role)

    async def apply_role(self, user_id: str, role: str) -> None:
        """Assign role ``role`` to ``user_id``'s enforced policy; see :func:`apply_role`."""
        await apply_role(user_id, role)

    async def remove_policy(self, user_id: str) -> None:
        """Delete a principal's policy AND principal row and revoke every key it owned; see :func:`delete_principal`."""
        await delete_principal(user_id)

    async def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        """Set/clear the disabled marker on ``user_id``'s principal; see :func:`set_principal_disabled`."""
        await set_principal_disabled(user_id, disabled)

    async def principal_roles(self, user_ids: Sequence[str]) -> Mapping[str, str | None]:
        """The role each principal holds; see :func:`principal_roles`."""
        return await principal_roles(user_ids)
