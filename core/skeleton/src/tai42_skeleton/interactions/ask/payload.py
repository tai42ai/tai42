"""Format-payload shaping and external-URL construction.

Turn the ask's answer-format arguments into the stored ``format_payload`` and resolve the
``link`` into the final external URL a human visits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel
from tai42_contract.interactions import AnswerFormat, FormData, FormPage

_CALLBACK_PLACEHOLDER = "{callback_url}"


def normalize_schema(schema: type[BaseModel] | dict[str, Any]) -> dict:
    """The JSON schema for ``schema`` — a pydantic model's, or a dict passed through."""
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    if isinstance(schema, dict):
        return schema
    raise ValueError("schema must be a pydantic model or a JSON-schema dict")


def build_payload(
    answer_format: AnswerFormat,
    options: list[str] | None,
    schema: type[BaseModel] | dict[str, Any] | None,
    url: str | None = None,
    verifier: dict[str, Any] | None = None,
    data: FormData | dict[str, Any] | None = None,
    pages: list[FormPage] | list[dict[str, Any]] | None = None,
) -> dict | None:
    """Build the stored ``format_payload`` for ``answer_format``, or ``None`` when it carries none."""
    if answer_format is AnswerFormat.SELECT:
        if not options:
            raise ValueError("answer_format 'select' requires options")
        return {"options": options}
    if answer_format is AnswerFormat.FORM:
        if schema is None:
            raise ValueError("answer_format 'form' requires a schema")
        form_payload: dict[str, Any] = {"schema": normalize_schema(schema)}
        # Per-send prefill/options and stepped pages ride the FORM payload as their
        # canonical dumps; the InteractionRequest validator cross-checks them against
        # the schema once (the one seam every ask door flows through).
        if data is not None:
            form_payload["data"] = (data if isinstance(data, FormData) else FormData.model_validate(data)).model_dump()
        if pages is not None:
            form_payload["pages"] = [
                (page if isinstance(page, FormPage) else FormPage.model_validate(page)).model_dump() for page in pages
            ]
        return form_payload
    if answer_format is AnswerFormat.EXTERNAL:
        # The URL exists only after the link is resolved, so this branch is called
        # after that step; schema (optional here) validates the callback payload.
        # A ``verifier`` (``{"name", "config"}``) rides the payload server-side so
        # the callback route can authenticate the signed server-to-server answer;
        # the client-facing serialization strips it (see ``routers.interactions``).
        payload: dict[str, Any] = {"url": url, **({"schema": normalize_schema(schema)} if schema is not None else {})}
        if verifier is not None:
            payload["verifier"] = verifier
        return payload
    if answer_format is AnswerFormat.TEXT and options:
        # TEXT suggested replies: unlike SELECT (a constrained answer set), these are
        # optional pre-filled answers a human MAY tap — a tapped option submits its own
        # text as the free-text answer, which validates as any string. Stored so the inbox
        # can render the chips and the channel delivery frame carries them alike.
        return {"options": options}
    return None


async def resolve_link(link: str | Callable[[str], Awaitable[str]], callback_url: str) -> str:
    """Turn the ``link`` argument into the final external URL the human visits."""
    if isinstance(link, str):
        if _CALLBACK_PLACEHOLDER not in link:
            raise ValueError(f"template link must contain {_CALLBACK_PLACEHOLDER}")
        # ``replace`` not ``format``: other braces in a real URL must survive.
        return link.replace(_CALLBACK_PLACEHOLDER, callback_url)
    # Callable flavor: it creates the external resource and returns its URL. An
    # exception from the builder propagates unchanged — nothing is persisted yet.
    final = await link(callback_url)
    if not isinstance(final, str) or not final.startswith(("http://", "https://")):
        raise ValueError(f"link builder must return an http(s) URL, got {final!r}")
    return final
