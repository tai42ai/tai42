"""The typed not-found error raised when a tool name is not registered on the
live server."""

from tai42_contract.errors import ErrorKind


class UnknownToolError(Exception):
    """Raised when a tool name is not registered on the live server.

    Carries the missing ``tool_name`` so a caller can build a typed 404/501
    without matching the message text — and so a catch can tell WHICH tool was
    missing.

    A tool body can itself dispatch by name, so an ``UnknownToolError`` escaping a
    RUN may name a DIFFERENT tool than the one the caller asked for; that inner
    failure must surface as its own error, never as "the requested tool does not
    exist". A door catching around a run therefore compares ``tool_name`` before
    answering a 404/501 about the requested tool. A LOOKUP
    (:meth:`ToolBinding.get_tool`, :meth:`ToolBinding.get_client_tools`) only ever
    raises for a name the caller itself asked for — the single requested name, or the
    first missing name of a requested list — so a catch around a lookup needs no such
    comparison."""

    # The requested tool name is not registered — a not-found target.
    __tai_error_kind__ = ErrorKind.NOT_FOUND

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"No such tool: {tool_name}.")
        self.tool_name = tool_name
