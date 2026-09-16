"""The principals CRUD half of the access-control store.

Split out of :mod:`tai42_skeleton.access_control.store` so each module stays within the
source-size budget. :class:`PrincipalsStoreMixin` is mixed into
:class:`~tai42_skeleton.access_control.store.PostgresAccessControlStore`, so
``access_control_store()`` exposes the policy and principal surfaces as ONE store over the
same component pool — the ``disabled`` writer touches both tables in one transaction, and
``self._write_policy_body`` (the policy half) is reachable here.

The Postgres transport seam is resolved through the ``store`` module
(``_store.client_ctx``) at call time, so a test that patches the policy store's transport
covers the principal surface too — there is one patch point for the whole store.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg.errors import UniqueViolation
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings

from tai42_skeleton.access_control import store as _store
from tai42_skeleton.db import SKELETON_COMPONENT

# The advisory key serializing the first-principal insert AND every last-admin-guarded
# mutation: the setup door's owner mint takes ``pg_advisory_xact_lock`` on it, counts
# principals, and inserts only when zero, so two concurrent setups can never both create an
# owner; :meth:`PrincipalsStoreMixin.principal_guard_txn` holds the SAME lock for a whole
# disable/delete transaction, so two concurrent removals of the last two enabled admins
# serialize and the second re-counts committed state. A stable arbitrary 64-bit constant
# distinct from any other advisory lock in the deployment.
_FIRST_PRINCIPAL_ADVISORY_LOCK = 0x4143_5052494E43  # "ACPRINC"

# The principal row shape every principal read/insert returns, in one place.
_PRINCIPAL_COLUMNS = "user_id, kind, display_name, created_by, disabled, created_at"


def _principal_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Assemble a principal dict from a ``_PRINCIPAL_COLUMNS`` row."""
    user_id, kind, display_name, created_by, disabled, created_at = row
    return {
        "user_id": user_id,
        "kind": kind,
        "display_name": display_name,
        "created_by": created_by,
        "disabled": disabled,
        "created_at": created_at,
    }


async def _count_other_enabled_admins_on_cursor(cur: Any, user_id: str) -> int:
    """Enabled admin principals OTHER than ``user_id``, counted on ``cur``.

    "Admin" is the shared :func:`~tai42_skeleton.access_control.user.is_admin_policy`
    predicate applied to each enabled principal's OWN policy row (a principal's own row has
    no owner, so ``owner_policy`` is ``None``): ``"*"`` scopes and no condition. Reusing the
    predicate keeps this count from drifting from the admin fence enforcement uses.
    """
    from tai42_contract.access_control.models import AccessPolicy

    from tai42_skeleton.access_control.user import is_admin_policy

    await cur.execute(
        "SELECT p.scopes, p.policy_data, p.condition "
        "FROM access_control_principals pr "
        "JOIN access_control_policies p ON p.user_id = pr.user_id "
        "WHERE pr.disabled = FALSE AND pr.user_id <> %s",
        (user_id,),
    )
    rows = await cur.fetchall()
    return sum(1 for row in rows if is_admin_policy(AccessPolicy(**_store._policy_body(row)), None))


async def _target_is_enabled_admin_on_cursor(cur: Any, user_id: str) -> bool:
    """Whether ``user_id``'s OWN policy row is admin-shaped, read on ``cur``.

    The same :func:`~tai42_skeleton.access_control.user.is_admin_policy` predicate the count
    applies, on the target's own row (no owner, so ``owner_policy`` is ``None``). ``False``
    when the target has no policy row. The caller pairs this with the target's ``disabled``
    state to decide whether removing it could strand the deployment with no enabled admin.
    """
    from tai42_contract.access_control.models import AccessPolicy

    from tai42_skeleton.access_control.user import is_admin_policy

    await cur.execute(
        "SELECT scopes, policy_data, condition FROM access_control_policies WHERE user_id = %s",
        (user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    return is_admin_policy(AccessPolicy(**_store._policy_body(row)), None)


async def _apply_disabled_on_cursor(store: Any, cur: Any, user_id: str, disabled: bool) -> dict[str, Any]:
    """Write the ``disabled`` marker to BOTH homes on ``cur`` and return the committed body.

    The single spelling of the ``disabled`` flip: it updates the AUTHORITATIVE
    ``access_control_principals.disabled`` column AND the enforcement projection
    ``access_control_policies.policy_data->'disabled'`` on the principal's own policy row, so
    the two can never drift. Runs on the caller's cursor, so the plain single-writer path and
    the advisory-locked guard share ONE implementation. Raises ``KeyError`` when the principal
    row or its policy row is absent (an invariant breach, never a silent no-op).
    """
    await cur.execute(
        "UPDATE access_control_principals SET disabled = %s WHERE user_id = %s",
        (disabled, user_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"cannot set disabled marker for unknown principal: {user_id!r}")
    await cur.execute(
        "SELECT scopes, policy_data, condition FROM access_control_policies WHERE user_id = %s FOR UPDATE",
        (user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise KeyError(f"principal {user_id!r} has no policy row to project 'disabled' onto")
    body = _store._policy_body(row)
    policy_data = dict(body["policy_data"])
    if disabled:
        policy_data["disabled"] = True
    else:
        policy_data.pop("disabled", None)
    body["policy_data"] = policy_data
    await store._write_policy_body(cur, user_id, body)
    return body


class _PrincipalGuard:
    """The last-admin re-read, count, and mutation, bound to one advisory-locked cursor.

    Runs as a single serialized transaction under :data:`_FIRST_PRINCIPAL_ADVISORY_LOCK`.
    Every method runs on the guard's own cursor, so the last-admin count and the mutation it
    gates commit or roll back together — never a count in one transaction and the write in
    another. The identity-provider revocation a delete also needs touches Redis and cannot
    join this transaction; the caller runs it AFTER the guarded transaction commits (see
    :func:`~tai42_skeleton.access_control.roles.delete_principal`).
    """

    def __init__(self, cur: Any, store: Any) -> None:
        self._cur = cur
        self._store = store

    async def read(self, user_id: str) -> dict[str, Any] | None:
        """The target principal row locked ``FOR UPDATE`` under the guard, or ``None`` when absent.

        The authoritative row the refusal and mutation decide on, never a pre-lock snapshot.
        """
        await self._cur.execute(
            f"SELECT {_PRINCIPAL_COLUMNS} FROM access_control_principals WHERE user_id = %s FOR UPDATE",  # noqa: S608 constant column list, not user input
            (user_id,),
        )
        row = await self._cur.fetchone()
        return _principal_row(row) if row is not None else None

    async def target_is_enabled_admin(self, user_id: str) -> bool:
        """Whether the target's own policy row is admin-shaped, read under the lock."""
        return await _target_is_enabled_admin_on_cursor(self._cur, user_id)

    async def count_other_enabled_admins(self, user_id: str) -> int:
        """Enabled admin principals OTHER than the target, counted under the lock."""
        return await _count_other_enabled_admins_on_cursor(self._cur, user_id)

    async def set_disabled(self, user_id: str, disabled: bool) -> dict[str, Any]:
        """Flip the target's ``disabled`` marker on both homes under the lock; return the committed body."""
        return await _apply_disabled_on_cursor(self._store, self._cur, user_id, disabled)

    async def delete(self, user_id: str) -> tuple[bool, bool]:
        """Delete the target's policy row and principal row under the lock.

        Returns ``(policy_existed, principal_existed)`` so the caller can surface an
        invariant breach (a principal with no policy row, or a row that vanished) loudly.
        """
        await self._cur.execute("DELETE FROM access_control_policies WHERE user_id = %s", (user_id,))
        policy_existed = self._cur.rowcount > 0
        await self._cur.execute("DELETE FROM access_control_principals WHERE user_id = %s", (user_id,))
        principal_existed = self._cur.rowcount > 0
        return policy_existed, principal_existed


class PrincipalsStoreMixin:
    """The principal CRUD methods of the access-control store."""

    async def create_principal(
        self, user_id: str, kind: str, display_name: str, created_by: str | None
    ) -> dict[str, Any]:
        """Insert a principal row and return it.

        The ``PRIMARY KEY (user_id)`` rejects a racing duplicate as the authority; a
        :class:`~psycopg.errors.UniqueViolation` surfaces as a loud ``ValueError`` rather
        than a silent second row.
        """
        async with (
            _store.client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            try:
                await cur.execute(
                    "INSERT INTO access_control_principals (user_id, kind, display_name, created_by) "
                    "VALUES (%s, %s, %s, %s) RETURNING user_id, kind, display_name, created_by, disabled, created_at",
                    (user_id, kind, display_name, created_by),
                )
                row = await cur.fetchone()
            except UniqueViolation as exc:
                raise ValueError(f"principal already exists: {user_id!r}") from exc
        assert row is not None  # noqa: S101 — INSERT ... RETURNING always yields the inserted row
        return _principal_row(row)

    async def create_first_principal(self, user_id: str, kind: str, display_name: str) -> dict[str, Any] | None:
        """Insert the FIRST principal (the setup door's owner) under an advisory lock, or ``None``.

        One transaction: take ``pg_advisory_xact_lock`` on :data:`_FIRST_PRINCIPAL_ADVISORY_LOCK`,
        count principals, and insert only when the count is zero. Returns the inserted row,
        or ``None`` when any principal already exists — the setup door's 409 signal. The
        owner has no creator, so ``created_by`` is ``NULL``.
        """
        async with (
            _store.client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_FIRST_PRINCIPAL_ADVISORY_LOCK,))
            await cur.execute("SELECT count(*) FROM access_control_principals")
            count_row = await cur.fetchone()
            if count_row is None or count_row[0] != 0:
                return None
            await cur.execute(
                "INSERT INTO access_control_principals (user_id, kind, display_name, created_by) "
                "VALUES (%s, %s, %s, NULL) RETURNING user_id, kind, display_name, created_by, disabled, created_at",
                (user_id, kind, display_name),
            )
            row = await cur.fetchone()
        assert row is not None  # noqa: S101 — INSERT ... RETURNING always yields the inserted row
        return _principal_row(row)

    @asynccontextmanager
    async def principal_guard_txn(self) -> AsyncIterator[_PrincipalGuard]:
        """One transaction under :data:`_FIRST_PRINCIPAL_ADVISORY_LOCK` for a last-admin-guarded mutation.

        The mutation is a disable or a delete. A concurrent guarded removal blocks at the lock
        and re-evaluates the admin count against committed state, so two removals of the last
        two enabled admins can never both pass. On the yielded guard the caller re-reads,
        counts, and mutates on this one cursor; the re-read, the last-admin refusal, and the
        write all commit or roll back together on block exit — a refusal leaves nothing
        written.
        """
        async with (
            _store.client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_FIRST_PRINCIPAL_ADVISORY_LOCK,))
            yield _PrincipalGuard(cur, self)

    async def get_principal(self, user_id: str) -> dict[str, Any] | None:
        """The principal row for ``user_id``, or ``None`` when none exists."""
        async with (
            _store.client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "SELECT user_id, kind, display_name, created_by, disabled, created_at "
                "FROM access_control_principals WHERE user_id = %s",
                (user_id,),
            )
            row = await cur.fetchone()
        return _principal_row(row) if row is not None else None

    async def list_principals(self) -> list[dict[str, Any]]:
        """Every principal row, ordered by ``created_at`` then ``user_id``."""
        async with (
            _store.client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "SELECT user_id, kind, display_name, created_by, disabled, created_at "
                "FROM access_control_principals ORDER BY created_at, user_id"
            )
            rows = await cur.fetchall()
        return [_principal_row(row) for row in rows]

    async def any_principal_exists(self) -> bool:
        """Whether ANY principal row exists — the ``needs_setup`` / setup-door 409 predicate."""
        async with (
            _store.client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("SELECT 1 FROM access_control_principals LIMIT 1")
            return await cur.fetchone() is not None

    async def update_principal_display_name(self, user_id: str, display_name: str) -> dict[str, Any] | None:
        """Update a principal's display name and return the row, or ``None`` when it is absent."""
        async with (
            _store.client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                "UPDATE access_control_principals SET display_name = %s WHERE user_id = %s "
                "RETURNING user_id, kind, display_name, created_by, disabled, created_at",
                (display_name, user_id),
            )
            row = await cur.fetchone()
        return _principal_row(row) if row is not None else None

    async def set_principal_disabled(self, user_id: str, disabled: bool) -> dict[str, Any]:
        """Flip a principal's disabled state, writing BOTH homes in one transaction.

        The single writer of the ``disabled`` marker for callers that do NOT need the
        last-admin guard (the backup restore path). It shares
        :func:`_apply_disabled_on_cursor` with the advisory-locked guard, so the two-home
        write has one implementation. Returns the committed policy body (for the caller to
        write into the enforcement cache). Raises ``KeyError`` when the principal row or its
        policy row is absent (an invariant breach, never a silent no-op).
        """
        async with (
            _store.client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            return await _apply_disabled_on_cursor(self, cur, user_id, disabled)

    async def delete_principal(self, user_id: str) -> bool:
        """Delete a principal row. Returns whether a row existed.

        The single-row delete for callers with no policy row or last-admin concern (the
        setup rollback and the create-principal compensation, which act on a row whose policy
        never landed). The last-admin-guarded delete of BOTH the principal and its policy row
        is :meth:`_PrincipalGuard.delete`.
        """
        async with (
            _store.client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute("DELETE FROM access_control_principals WHERE user_id = %s", (user_id,))
            return cur.rowcount > 0
