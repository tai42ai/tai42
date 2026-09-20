"""HTTP surface for sandbox identity + the resolved policy (AUTHED).

- ``GET /api/sandbox`` — sandbox identity plus the resolved security-as-config policy
  (or ``present: false`` with the policy still present, 200, so the UI renders the empty
  state without treating it as an error).

There are NO mutation routes: sessions are created only by in-process consumers
(agents/tools through ``require_sandbox()``), so there is no HTTP session-CRUD surface and
no ``501-not-configured`` door — the sandbox is not a DB-gated feature. The door is a thin
adapter over ``tai42_skeleton.operations.sandbox``.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.operations import operation_metadata_of, register_operation_route
from tai42_skeleton.operations.sandbox import sandbox_info as _sandbox_info_op

sandbox_info = register_operation_route(
    tai42_app,
    operation_metadata_of(_sandbox_info_op),
    path="/api/sandbox",
    method="GET",
    action="read",
)
