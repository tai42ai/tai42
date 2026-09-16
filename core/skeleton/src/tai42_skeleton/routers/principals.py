"""The admin-only principal-management doors under ``/api/auth/principals``.

``GET`` (list) is an admin-only ``secret`` read; ``POST`` (create), ``PUT`` (update) and
``DELETE`` are admin-only ``fenced`` mutations — the same action-class fence the role
doors carry. The action-class is the gate fence; the op-level ``require_admin`` is defense
in depth behind it. The routes sit under the reserved ``/api/auth`` namespace, so the
control-plane gate admin-gates them with no per-deployment route row.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.operations import operation_metadata_of, register_operation_route
from tai42_skeleton.operations.principals import create_principal as _create_principal_op
from tai42_skeleton.operations.principals import delete_principal as _delete_principal_op
from tai42_skeleton.operations.principals import list_principals as _list_principals_op
from tai42_skeleton.operations.principals import update_principal as _update_principal_op

list_principals = register_operation_route(
    tai42_app, operation_metadata_of(_list_principals_op), path="/api/auth/principals", method="GET", action="secret"
)

create_principal = register_operation_route(
    tai42_app, operation_metadata_of(_create_principal_op), path="/api/auth/principals", method="POST", action="fenced"
)

update_principal = register_operation_route(
    tai42_app,
    operation_metadata_of(_update_principal_op),
    path="/api/auth/principals/{user_id}",
    method="PUT",
    action="fenced",
)

delete_principal = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_principal_op),
    path="/api/auth/principals/{user_id}",
    method="DELETE",
    action="fenced",
)
