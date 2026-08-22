"""The builtin ``ask_user`` tool: a thin, LLM-facing shim over the interactions
feature's ``ask_user`` helper. It lets an agent pause mid-run to ask a human a
question — in ``mode="sync"`` it blocks until the answer (or a timeout) returns;
in ``mode="async"`` it PARKS, returning a ``SuspendedInteraction`` at once, and a
later answer/expiry resumes the agent out of band.
"""

from datetime import datetime
from typing import Any, Literal

from tai42_contract.app import tai42_app

from tai42_skeleton.interactions import ask_user as _ask_user

# The answer is returned as ``Any`` — a scalar (text→str, confirm→bool,
# select→str) or a dict (external/JSON formats) — so FastMCP derives no output
# schema and a scalar answer would reach the caller only as unstructured
# ``content``, never ``structuredContent``/``result.data`` (a dict answer already
# populates it). Advertise a permissive wrap schema so EVERY answer surfaces in
# ``result.data``: the server wraps it as ``{"result": <answer>}`` and the client
# unwraps it back (a string stays a string, a dict stays a dict).
_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {"title": "Result"}},
    "x-fastmcp-wrap-result": True,
}


@tai42_app.tools.tool(output_schema=_ANSWER_SCHEMA, tags={"interactions"})
async def ask_user(
    question: str,
    answer_format: str = "text",
    options: list[str] | None = None,
    schema: dict[str, Any] | None = None,
    group_id: str | None = None,
    timeout: float | None = None,
    link: str | None = None,
    channel: str | None = None,
    recipient: str | None = None,
    audience: str | None = None,
    media: list[dict[str, Any]] | None = None,
    mode: Literal["sync", "async"] = "sync",
    expiry_at: datetime | None = None,
) -> Any:
    """Ask a human a question mid-run: in "sync" mode block until they answer; in
    "async" mode park the caller and return a suspension sentinel immediately.

    Args:
        question: The question shown to the human.
        answer_format: How the answer is collected and typed:
            - "text": free-form text, returned as a string.
            - "confirm": a yes/no choice, returned as a bool.
            - "select": one value from ``options``, the chosen string returned.
            - "form": a structured object validated against ``schema``, returned
              as a dict.
            - "external": the human acts on an EXTERNAL surface (sign, approve,
              pay) reached through ``link``, and the external system delivers the
              answer through a public callback URL. Returns the callback payload.
        options: The allowed values; required for "select".
        schema: A JSON schema describing the answer object; required for "form",
            optional for "external" (validates the callback payload). A "form"
            delivered over a ``channel`` must use the channel-deliverable subset
            (it is answered on a server-rendered page): root object with a
            non-empty ``properties`` map, every property a scalar
            (string/boolean/integer/number), ``enum`` only on a string property
            (a non-empty list of strings), and ``required`` naming only declared
            properties. A richer schema is rejected before the question is stored.
        group_id: An optional thread key grouping related questions.
        timeout: Seconds to wait before raising; defaults to the configured
            interactions timeout.
        link: Required for "external", forbidden otherwise. A URL template
            containing the literal placeholder ``{callback_url}``, which is
            substituted with the public callback URL before the human visits it.
        channel: Optional name of a registered delivery channel that pushes the
            question to a human on an external medium (e.g. a chat or SMS
            channel) and bridges the reply back. Omit for the default inbox
            surface. Forbids ``link`` — the channel owns delivery. A ``form``
            question is delivered only over a channel that advertises form
            delivery; a channel without that capability refuses it loudly, and its
            ``schema`` must use the channel-deliverable subset (see ``schema``).
            An unknown name is rejected before the question is stored.
        recipient: Optional per-call address (chat id, phone number, ...) the
            named channel sends to; the channel validates it against its
            operator allowlist and refuses an unlisted address. Omit to use
            the channel's operator-configured default recipient. Requires
            ``channel`` — an address is meaningless without one.
        audience: The identity (user_id) this question is addressed to; a
            restricted identity sees/answers only questions addressed to it.
            Leave unset for an operator/broadcast question. Distinct from
            ``recipient``, which is a channel delivery address, not an identity.
        media: Optional display-only images and links shown WITH the question in
            the Studio inbox; each item is an object
            ``{"kind": "image"|"link", "url": ..., "caption": ...?}``. For an
            ``image`` the url is an absolute ``https`` URL or a ``data:image/*``
            URI (remote images are https-only — the inbox CSP blocks ``http:``
            images); for a ``link`` it is an absolute ``http(s)`` URL. A
            ``data:image/*`` URI is stored by the platform and the record keeps a
            served reference to it, not the inline bytes. ``caption`` is the image
            alt text or link label. The item count is bounded by a loose platform
            guard, within a per-request total URI budget. Media is display-only —
            the human still answers via ``answer_format`` — and is NOT forwarded to
            channel deliveries (a channel receives the question text only). Invalid
            media fails the call before the question is stored.
        mode: The wait discipline. "sync" (the default) blocks and returns the
            typed answer. "async" PARKS the caller: it stores (and optionally
            delivers) the question but returns a suspension sentinel immediately,
            and a later answer or expiry resumes the run's driver out of band.
            An "async" ask requires a resuming driver bound by the engine.
        expiry_at: The async park deadline — when the parked question expires.
            Only valid with mode="async" and mutually exclusive with ``timeout``.

    Returns:
        The typed answer (text -> str, confirm -> bool, select -> chosen value,
        form -> validated dict, external -> the callback payload).

    Raises:
        InteractionTimeoutError: No answer arrived before the timeout elapsed.
        InteractionLimitError: Too many questions are already open (the
            ``max_concurrent`` guard tripped).
        ValueError: The ``answer_format``, its argument combination, the
            ``channel`` combination, or the ``audience`` (blank) is invalid.
        CrossIdentityAudienceError: A restricted caller addressed an ``audience``
            other than its own identity — a loud cross-identity authorization
            denial (the write-side mirror of the answer door's 403).
        RuntimeError: An ``external`` question (or one bound to a ``channel``)
            was asked without ``INTERACTIONS_PUBLIC_BASE_URL`` configured.
    """
    return await _ask_user(
        question,
        answer_format=answer_format,
        options=options,
        schema=schema,
        group_id=group_id,
        timeout=timeout,
        link=link,
        channel=channel,
        recipient=recipient,
        audience=audience,
        # A ``list[dict]`` is a valid ``list[MediaItem | dict]`` argument; the
        # invariant-list check is a type-system limitation, not a runtime one.
        media=media,  # type: ignore[arg-type]
        mode=mode,
        expiry_at=expiry_at,
    )
