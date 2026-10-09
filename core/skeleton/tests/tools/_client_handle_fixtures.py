"""Test-only tools a neutral in-process engine drives through ``get_client_tools`` handles."""

from __future__ import annotations

from fastmcp.dependencies import Depends
from fastmcp.server.context import Context
from tai42_contract.app import tai42_app
from tai42_contract.interactions import RunTerminalFailed
from tai42_contract.secrets import SecretValue
from tests.tools._client_handle_counter import BODY_RUNS, OVERLONG_NAME


def _count(name: str) -> None:
    BODY_RUNS[name] = BODY_RUNS.get(name, 0) + 1


@tai42_app.tools.tool
def five_fields(a: int, b: str, c: float = 1.0, d: bool = False, e: list[str] | None = None) -> dict:
    """Echo five typed fields."""
    _count("five_fields")
    return {"a": a, "b": b, "c": c, "d": d, "e": e}


@tai42_app.tools.tool
async def with_context(q: str, ctx: Context) -> str:
    """Report the injected context's type beside the query."""
    return f"{q}:{type(ctx).__name__}"


def _scale() -> int:
    return 3


@tai42_app.tools.tool
def with_dependency(n: int, factor: int = Depends(_scale)) -> int:
    """Multiply by an injected factor."""
    return n * factor


@tai42_app.tools.tool
def holds_a_secret() -> dict:
    """Return a wrapped secret."""
    return {"token": SecretValue("tok-4242-xyzzy")}


@tai42_app.tools.tool
def body_fails(q: str) -> str:
    """Raise from the body."""
    _count("body_fails")
    raise ValueError(f"boom {q}")


@tai42_app.tools.tool
def terminal_fails() -> str:
    """Reach a FAILED terminal."""
    raise RunTerminalFailed({"status": "failed", "reason": "the step failed"})


@tai42_app.tools.tool
def unencodable() -> dict:
    """Return a lone UTF-16 surrogate."""
    return {"text": "bad \ud800 text"}


@tai42_app.tools.tool(name=OVERLONG_NAME)
def overlong() -> str:
    """A tool registered under a name longer than a bound tool name may be."""
    return "never bound"
