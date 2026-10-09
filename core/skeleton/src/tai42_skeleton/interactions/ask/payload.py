"""Format-payload shaping and external-URL construction.

Turn the ask's answer-format arguments into the stored ``format_payload`` and resolve the
``link`` into the final external URL a human visits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel
from tai42_contract.interactions import AnswerFormat, FormData, FormPage, FormReactions

_CALLBACK_PLACEHOLDER = "{callback_url}"


def normalize_schema(schema: type[BaseModel] | dict[str, Any]) -> dict:
    """The JSON schema for ``schema`` — a pydantic model's, or a dict passed through."""
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_json_schema()
    if isinstance(schema, dict):
        return schema
    raise ValueError("schema must be a pydantic model or a JSON-schema dict")


def _build_form_payload(
    schema: type[BaseModel] | dict[str, Any] | None,
    data: FormData | dict[str, Any] | None,
    pages: list[FormPage] | list[dict[str, Any]] | None,
    reactions: FormReactions | dict[str, Any] | None,
) -> dict[str, Any]:
    # The FORM payload: the normalized schema plus the per-send prefill/options, stepped pages
    # and reaction triggers as their canonical dumps. The InteractionRequest validator
    # cross-checks them against the schema once (the one seam every ask door flows through).
    if schema is None:
        raise ValueError("answer_format 'form' requires a schema")
    form_payload: dict[str, Any] = {"schema": normalize_schema(schema)}
    if data is not None:
        form_payload["data"] = (data if isinstance(data, FormData) else FormData.model_validate(data)).model_dump()
    if pages is not None:
        form_payload["pages"] = [
            (page if isinstance(page, FormPage) else FormPage.model_validate(page)).model_dump() for page in pages
        ]
    if reactions is not None:
        form_payload["reactions"] = (
            reactions if isinstance(reactions, FormReactions) else FormReactions.model_validate(reactions)
        ).model_dump()
    return form_payload


def answer_payload(
    answer_format: AnswerFormat,
    options: list[str] | None,
    schema: type[BaseModel] | dict[str, Any] | None,
    data: FormData | dict[str, Any] | None = None,
    pages: list[FormPage] | list[dict[str, Any]] | None = None,
    reactions: FormReactions | dict[str, Any] | None = None,
) -> dict | None:
    """The answer part of the stored ``format_payload`` — what an answer is checked against — or ``None``."""
    if answer_format is AnswerFormat.SELECT:
        if not options:
            raise ValueError("answer_format 'select' requires options")
        return {"options": options}
    if answer_format is AnswerFormat.FORM:
        return _build_form_payload(schema, data, pages, reactions)
    if answer_format in (AnswerFormat.FREE, AnswerFormat.EXTERNAL):
        # The answer is any JSON value, checked against the schema when one is given.
        return {"schema": normalize_schema(schema)} if schema is not None else None
    if answer_format is AnswerFormat.TEXT and options:
        # TEXT suggested replies: unlike SELECT (a constrained answer set), these are
        # optional pre-filled answers a human MAY tap — a tapped option submits its own
        # text as the free-text answer, which validates as any string. Stored so the inbox
        # can render the chips and the channel delivery frame carries them alike.
        return {"options": options}
    return None


def build_payload(
    answer_format: AnswerFormat,
    options: list[str] | None,
    schema: type[BaseModel] | dict[str, Any] | None,
    url: str | None = None,
    verifier: dict[str, Any] | None = None,
    data: FormData | dict[str, Any] | None = None,
    pages: list[FormPage] | list[dict[str, Any]] | None = None,
    reactions: FormReactions | dict[str, Any] | None = None,
) -> dict | None:
    """Build the stored ``format_payload`` for ``answer_format``, or ``None`` when it carries none.

    The answer part (:func:`answer_payload`) plus, for EXTERNAL, its delivery keys: the ``url``
    the human visits (always present) and an optional ``verifier`` (``{"name", "config"}``) the
    callback route authenticates the signed server-to-server answer with — the client-facing
    serialization strips it (see ``routers.interactions``). The URL exists only after the link is
    resolved, so EXTERNAL is built after that step.
    """
    answer = answer_payload(answer_format, options, schema, data=data, pages=pages, reactions=reactions)
    if answer_format is not AnswerFormat.EXTERNAL:
        return answer
    payload: dict[str, Any] = {"url": url, **(answer or {})}
    if verifier is not None:
        payload["verifier"] = verifier
    return payload


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
