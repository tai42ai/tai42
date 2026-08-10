"""Shared harness for the remote command-group tests.

Every remote command talks to the ``/api/*`` surface through the shared
:class:`ApiClient`. These helpers stand a FAKE server up behind an httpx
``MockTransport`` — no real network — and invoke the compiled ``tai`` app with the
client pointed at it, so a command's request shaping, envelope handling, and typed
errors are all exercised against controlled responses.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

import httpx
from click.testing import CliRunner, Result

from tai42_cli import app as app_module
from tai42_cli.client import ApiClient
from tai42_cli.context import AppContext

Handler = Callable[[httpx.Request], httpx.Response]

# CSI sequences (colour/style) Click's rich formatter emits when the runner
# forces colour; stripping them lets content assertions read the visible text.
_ANSI_CSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Drop ANSI escape sequences so an assertion sees the plain visible text."""
    return _ANSI_CSI.sub("", text)


# Force a wide, plain terminal for every invoke: Typer/rich renders its error and help
# panels through a rich Console that, off a real tty, folds a long option name mid-token at
# whatever narrow width it detects (empty COLUMNS collapses to a handful of columns in CI),
# so a substring assert like ``"--params-file" in output`` breaks. COLUMNS pins the width
# wide enough that no option wraps; NO_COLOR and TERM keep the output free of ANSI styling.
_PLAIN_WIDE_TERM = {"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"}


def data_response(payload: Any, status_code: int = 200) -> httpx.Response:
    """A ``{"data": ...}`` success envelope response."""
    return httpx.Response(status_code, json={"data": payload})


def error_response(message: str, status_code: int) -> httpx.Response:
    """An ``{"error": ...}`` failure envelope response."""
    return httpx.Response(status_code, json={"error": message})


def sse_response(frames: Iterable[Any], *, content_type: str = "text/event-stream") -> httpx.Response:
    """An SSE response whose ``data:`` frames carry the JSON-encoded ``frames``."""
    import json

    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    return httpx.Response(200, headers={"content-type": content_type}, content=body.encode())


def run_cli(
    monkeypatch,
    handler: Handler,
    args: list[str],
    *,
    json_output: bool = False,
    stdin: str | bytes | None = None,
) -> Result:
    """Invoke the compiled ``tai`` app against a fake server built from ``handler``.

    The shared client is redirected onto an httpx ``MockTransport`` wrapping
    ``handler`` (which returns the canned response per request), so no request
    leaves the process. ``stdin`` feeds the command's standard input for the
    stdin-sourced flags.
    """
    transport = httpx.MockTransport(handler)

    def _client(self: AppContext, *, anonymous: bool = False) -> ApiClient:
        return ApiClient(self.server_url, None if anonymous else self.api_key, transport=transport)

    monkeypatch.setattr(AppContext, "client", _client)
    monkeypatch.setenv("TAI_API_KEY", "test-key")
    monkeypatch.setenv("TAI_SERVER_URL", "http://testserver")
    full_args = (["--json"] if json_output else []) + args
    return CliRunner().invoke(app_module.app, full_args, input=stdin, env=_PLAIN_WIDE_TERM)
