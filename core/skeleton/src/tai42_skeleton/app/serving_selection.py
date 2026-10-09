"""The MCP transport a process serves, as its boot selected it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Transport = Literal["http", "streamable-http", "sse"]


@dataclass(frozen=True)
class ServingSelection:
    """The MCP transport a process serves: the transport family and, for streamable HTTP, statelessness."""

    transport: Transport
    stateless_http: bool

    @property
    def stateless_http_arg(self) -> bool | None:
        """The ``stateless_http`` argument every streamable-HTTP builder takes for this selection.

        ``True`` forces stateless; ``None`` leaves it to fastmcp's own process setting, exactly
        as a boot that did not ask for statelessness resolves it.
        """
        return True if self.stateless_http else None
