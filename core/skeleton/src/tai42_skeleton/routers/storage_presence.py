"""The always-mounted storage presence read — ``GET /api/storage`` (AUTHED).

An AUTHED thin adapter over ``operations.storage.storage_info``. It answers the
deployment fact "is a storage provider installed?" — the registered provider's
identity, or ``{"present": false, "provider": null, "module": null}`` (a ``200``)
when none is installed. This is the core tier of ``route_defaults.CORE_API_ROUTERS``:
force-mounted on every boot, so a consumer can read presence in every deployment —
including one that mounts no storage MANAGEMENT surface — and distinguish "no
provider" from "cannot ask". The management routes (``/api/storage/resources*``,
``/api/storage/dirs*``, the content download) stay in the optional ``storage`` router.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.operations import operation_metadata_of, register_operation_route
from tai42_skeleton.operations.storage import storage_info as _storage_info_op

storage_info = register_operation_route(
    tai42_app,
    operation_metadata_of(_storage_info_op),
    path="/api/storage",
    method="GET",
    action="read",
)
