"""Every stamped SKELETON exception resolves to its stable ``ErrorKind``, and a
projected op-tool ``ToolError`` recovers the underlying operation's kind through
the ``__cause__`` chain.

The projection raises ``ToolError(exc.message) from exc`` (see
``operations/projection.py``), erasing the operation's type at the tool edge; the
``__cause__`` walk in :func:`error_kind` is what lets a tool-edge consumer still
classify the failure. This pins that end to end.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp.exceptions import ToolError
from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.manifest import ApiToolsConfig

from tai42_skeleton.exceptions.exceptions import TaiValidationError, TurnTimeoutError
from tai42_skeleton.interactions.helper import InteractionLimitError, InteractionTimeoutError
from tai42_skeleton.operations import OperationRegistry, operation
from tai42_skeleton.operations.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    NotSupportedError,
    OperationError,
    OperationFailed,
    PayloadTooLargeError,
    PermissionDenied,
    UnavailableError,
    UpstreamError,
    ValidationRejected,
)
from tai42_skeleton.operations.projection import project_operations
from tai42_skeleton.tools.binding import UnknownToolError

_STAMPED_SKELETON_ERRORS: list[tuple[BaseException, ErrorKind]] = [
    # operations/errors.py — beside each declared HTTP status
    (OperationError("x"), ErrorKind.UPSTREAM_ERROR),
    (ValidationRejected("x"), ErrorKind.BAD_INPUT),
    (BadRequestError("x"), ErrorKind.BAD_INPUT),
    (PayloadTooLargeError("x"), ErrorKind.BAD_INPUT),
    (NotFoundError("x"), ErrorKind.NOT_FOUND),
    (PermissionDenied("x"), ErrorKind.UNAUTHORIZED),
    (ForbiddenError("x"), ErrorKind.UNAUTHORIZED),
    (ConflictError("x"), ErrorKind.CONFLICT),
    (NotSupportedError("x"), ErrorKind.UNAVAILABLE),
    (UpstreamError("x"), ErrorKind.UPSTREAM_ERROR),
    (UnavailableError("x"), ErrorKind.UNAVAILABLE),
    (OperationFailed("x"), ErrorKind.UPSTREAM_ERROR),
    # exceptions/exceptions.py
    (TaiValidationError("x"), ErrorKind.BAD_INPUT),
    (TurnTimeoutError("x"), ErrorKind.TIMED_OUT),
    # tools/binding.py
    (UnknownToolError("t"), ErrorKind.NOT_FOUND),
    # interactions/helper.py
    (InteractionTimeoutError("x"), ErrorKind.TIMED_OUT),
    (InteractionLimitError("x"), ErrorKind.UNAVAILABLE),
]


@pytest.mark.parametrize(
    ("exc", "expected"),
    _STAMPED_SKELETON_ERRORS,
    ids=lambda v: v if isinstance(v, ErrorKind) else type(v).__name__,
)
def test_stamped_skeleton_exception_resolves_to_its_kind(exc: BaseException, expected: ErrorKind):
    assert error_kind(exc) is expected


class _RecordingTools:
    def __init__(self) -> None:
        self.registered: dict[str, dict] = {}

    def tool(self, *, force, name, tags, annotations):
        def decorator(func):
            self.registered[name] = {"func": func}
            return func

        return decorator


class _FakeApp:
    def __init__(self) -> None:
        self.tools = _RecordingTools()


def test_projected_tool_error_resolves_via_cause():
    # Mirror test_projection.py: a projected op that raises a typed OperationError
    # surfaces a ToolError at the tool edge — but ``error_kind`` follows ``__cause__``
    # back to the operation's kind.
    reg = OperationRegistry()

    @operation(name="boom", summary="s", tags=["t"], errors=[ConflictError], registry=reg)
    async def boom():
        raise ConflictError("nope")

    app = _FakeApp()
    project_operations(app, ApiToolsConfig(), registry=reg)
    wrapper = app.tools.registered["boom"]["func"]

    with pytest.raises(ToolError) as caught:
        asyncio.run(wrapper())

    tool_error = caught.value
    # The tool edge erased the type — a bare ToolError alone is unclassifiable...
    assert error_kind(ToolError("nope")) is ErrorKind.UNKNOWN
    # ...but the projected one carries the ConflictError as its cause.
    assert tool_error.__cause__ is not None
    assert error_kind(tool_error) is ErrorKind.CONFLICT
