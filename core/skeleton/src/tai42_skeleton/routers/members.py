"""The admin-only Members listing door under ``/api/auth/members``.

``GET`` aggregates every registered accounts provider's people and outstanding
invitations into one deployment-wide view (an admin-only ``secret`` read — the same
action-class fence the principals listing carries). The action-class is the gate fence;
the op-level ``require_admin`` is defense in depth behind it. The route sits under the
reserved ``/api/auth`` namespace, so the control-plane gate admin-gates it with no
per-deployment route row.

It is a thin adapter over :func:`tai42_skeleton.operations.members.list_members`.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.operations import operation_metadata_of, register_operation_route
from tai42_skeleton.operations.members import list_members as _list_members_op

list_members = register_operation_route(
    tai42_app, operation_metadata_of(_list_members_op), path="/api/auth/members", method="GET", action="secret"
)
