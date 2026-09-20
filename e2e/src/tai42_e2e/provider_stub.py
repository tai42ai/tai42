"""Shared building blocks for the recording provider stubs: the ready-to-POST
signed inbound request the stubs synthesize, and the loud catch-all an
unscripted provider call falls through to."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class SignedInbound:
    """A ready-to-POST inbound request the channel stubs synthesize: the headers
    (carrying the genuine signature) and the exact raw body bytes the signature
    was computed over."""

    headers: dict[str, str]
    body: bytes


def _install_catch_all(app: FastAPI, provider: str) -> None:
    """Answer any unscripted path with a loud 500 — an unexpected provider call
    must fail the test, never be silently absorbed (the LlmStub unscripted rule)."""

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def unexpected(path: str, request: Request) -> JSONResponse:
        return JSONResponse(
            {"error": f"fake_{provider}: unexpected {request.method} /{path}"},
            status_code=500,
        )
