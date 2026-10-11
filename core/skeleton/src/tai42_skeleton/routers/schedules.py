"""HTTP surface for the scheduling feature — the Studio's schedules UI doors.

Four doors, all AUTHED, thin adapters over operations in
``tai42_skeleton.operations.schedules``:

- ``GET /api/schedules`` — list schedules via ``backend_list_schedules``.
- ``GET /api/schedules/server-datetime`` — the server's current time, read from the
  platform's own clock (independent of any scheduling backend).
- ``POST /api/schedules`` — schedule a caller-chosen tool run; the body is the
  contract's ``ScheduleCreate``: the base ``tool_name``, its ``tool_kwargs``, and an
  optional typed ``cadence`` (none runs the tool once now) and ``schedule_name``.
- ``DELETE /api/schedules/{schedule_name}`` — remove a schedule by name via
  ``backend_delete_schedule``.

Availability is detected at REQUEST time, never probed at import. When no installed
backend registers the scheduling marker tools the list/create/delete doors answer a
loud 501; ``server-datetime`` needs no backend and always answers from the platform's
own clock. Success bodies are ``{"data": ...}``; failures are ``{"error": "<message>"}``.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_contract.backend import ScheduleCreate

from tai42_skeleton.operations import operation_metadata_of, register_operation_route
from tai42_skeleton.operations.adapter import read_json_object, validate_body
from tai42_skeleton.operations.schedules import create_schedule as _create_schedule_op
from tai42_skeleton.operations.schedules import delete_schedule as _delete_schedule_op
from tai42_skeleton.operations.schedules import list_schedules as _list_schedules_op
from tai42_skeleton.operations.schedules import server_datetime as _server_datetime_op


async def _extract_create(request: Request) -> dict[str, Any]:
    """Parse and validate the create body at the HTTP edge into the operation's flat kwargs.

    The WHOLE body validates through the contract's :class:`ScheduleCreate` (a 400 naming each violated
    rule; an unknown key such as ``schedule_kwargs`` is refused), so every field the model carries — the
    cadence, the ``execution_key`` and the four door-contract jqs included — reaches the operation. The
    returned kwargs are the model's own fields read as their live values, so a nested cadence, binding
    or jq is forwarded parsed, never lowered to a dict.
    """
    body = await read_json_object(request)
    model = validate_body(ScheduleCreate, body, "schedule create params")
    return {name: getattr(model, name) for name in ScheduleCreate.model_fields}


list_schedules = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_schedules_op),
    path="/api/schedules",
    method="GET",
    action="read",
)

server_datetime = register_operation_route(
    tai42_app,
    operation_metadata_of(_server_datetime_op),
    path="/api/schedules/server-datetime",
    method="GET",
    action="read",
)

create_schedule = register_operation_route(
    tai42_app,
    operation_metadata_of(_create_schedule_op),
    path="/api/schedules",
    method="POST",
    context_extractor=_extract_create,
    action="write",
)

delete_schedule = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_schedule_op),
    path="/api/schedules/{schedule_name}",
    method="DELETE",
    action="write",
)
