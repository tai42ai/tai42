"""tai42-contract — pure interface contracts for the TAI ecosystem.

Protocols, ABCs, and pydantic models only. No logic. At runtime this package
imports nothing but ``pydantic``; vendor types are ``TYPE_CHECKING``-only.

The primary entry points are re-exported here: the assembled ``TaiApp`` facade
and its runtime forwarding handle ``tai42_app``, plus the names most consumers
need (``Agent``, ``Manifest``, ``ToolInfo``, ``Storage``, ``Backend``, and the
shared exception types). Everything else stays in its subpackage.
"""

from __future__ import annotations

from importlib.metadata import version

from tai42_contract.agent import Agent
from tai42_contract.app import DeclaredRouteMetadata, RouteAction, TaiApp, tai42_app
from tai42_contract.backend import Backend
from tai42_contract.errors import (
    ERROR_KIND_ATTR,
    ClientConnectError,
    ClientDisconnectedError,
    ErrorKind,
    error_kind,
    register_error_kind,
)
from tai42_contract.manifest import Manifest
from tai42_contract.storage import Storage
from tai42_contract.tools import ToolInfo

__version__ = version("tai42-contract")

__all__ = [
    "ERROR_KIND_ATTR",
    "Agent",
    "Backend",
    "ClientConnectError",
    "ClientDisconnectedError",
    "DeclaredRouteMetadata",
    "ErrorKind",
    "Manifest",
    "RouteAction",
    "Storage",
    "TaiApp",
    "ToolInfo",
    "__version__",
    "error_kind",
    "register_error_kind",
    "tai42_app",
]
