"""Shared tool-call request parsing for the tool doors.

Lives in its own ROUTE-FREE module (no ``@tai42_app.http.custom_route`` decorators)
so the synchronous ``/api/run-tool`` door and the background ``/api/tool-runs``
submit door can share the parser WITHOUT importing each other's route module —
importing a route module registers its routes as a side effect, so a shared
helper must not sit in one.
"""

from __future__ import annotations

from pydantic import ValidationError
from starlette.requests import Request
from tai42_contract.states import StateSubject


class ToolCallRequestError(Exception):
    """A malformed tool-call request body — carries the loud ``(message, status_code)`` the door returns.

    Shared by the synchronous ``/api/run-tool`` door and the background ``/api/tool-runs`` submit door so
    both reject a bad body identically.
    """

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def read_tool_call(request: Request) -> tuple[str, dict[str, object], StateSubject | None]:
    """Parse and validate a tool-call body ``{tool_name, arguments, subject}``.

    Both tool-execution doors share this one field shape — the explicit fields that
    match the operation's request model. Returns ``(tool_name, arguments, subject)``;
    ``arguments`` defaults to ``{}`` and ``subject`` to ``None`` when absent. Raises
    :class:`ToolCallRequestError` on invalid JSON, a non-object body, a
    missing/empty ``tool_name``, a non-object ``arguments``, or a ``subject`` that
    is not a valid :class:`~tai42_contract.states.StateSubject` — the caller maps
    it to the same loud 4xx both doors share.
    """
    try:
        body = await request.json()
    except ValueError as exc:
        raise ToolCallRequestError("invalid JSON body", 400) from exc
    if not isinstance(body, dict):
        raise ToolCallRequestError("body must be a JSON object", 400)
    name = body.get("tool_name", "")
    if not isinstance(name, str) or not name:
        raise ToolCallRequestError("body must contain a non-empty 'tool_name'", 400)
    arguments = body.get("arguments", {})
    # ``arguments`` feeds the tool's validated kwargs; a non-object (array/scalar)
    # is a malformed request — reject it as a loud 400 here rather than letting it
    # fail deeper as a 500.
    if not isinstance(arguments, dict):
        raise ToolCallRequestError("'arguments' must be a JSON object", 400)
    return name, arguments, _read_subject(body.get("subject"))


def _read_subject(raw: object) -> StateSubject | None:
    """Validate the body's optional ``subject`` into a :class:`StateSubject`.

    Absent or ``null`` yields ``None`` — a caller who names no subject leaves an async
    park un-indexed, exactly as when no subject can be named. Any present value is
    validated against the contract model; a malformed one (a non-object, an unknown
    ``target_kind``, a bad ``kind``/``key``) is a loud 400, never silently dropped.
    """
    if raw is None:
        return None
    try:
        return StateSubject.model_validate(raw)
    except ValidationError as exc:
        raise ToolCallRequestError(f"invalid 'subject': {exc}", 400) from exc
