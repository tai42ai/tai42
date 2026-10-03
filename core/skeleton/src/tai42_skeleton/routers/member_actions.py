"""The admin-only member-actions doors under ``/api/auth/member-actions``.

``GET /api/auth/member-actions`` lists the catalog of declared member actions across every
registered accounts provider (an admin-only ``secret`` read — the same fence the members and
principals listings carry). ``POST /api/auth/member-actions/invoke`` invokes one action by
its opaque key (an admin-only ``fenced`` write). Both sit under the reserved ``/api/auth``
namespace, so the control-plane gate admin-gates them with no per-deployment route row; the
op-level ``require_admin`` is defense in depth behind the fence.

Thin adapters over :mod:`tai42_skeleton.operations.member_actions`.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.operations import operation_metadata_of, register_operation_route
from tai42_skeleton.operations.member_actions import invoke_member_action as _invoke_member_action_op
from tai42_skeleton.operations.member_actions import list_member_actions as _list_member_actions_op

list_member_actions = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_member_actions_op),
    path="/api/auth/member-actions",
    method="GET",
    action="secret",
)

invoke_member_action = register_operation_route(
    tai42_app,
    operation_metadata_of(_invoke_member_action_op),
    path="/api/auth/member-actions/invoke",
    method="POST",
    action="fenced",
)
