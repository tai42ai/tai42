"""The ``ask_user`` author surface — a typed-callable Protocol.

``AskUser`` is the engine-agnostic human-in-the-loop entry point: an awaitable
called with a ``question`` (and an optional answer shape). In ``mode="sync"`` it
blocks until a human answers and returns the typed answer; in ``mode="async"``
it PARKS the caller and returns a sentinel, and a later answer/expiry resumes
work out of band. An implementation persists the question, blocks on (or parks
against) a reply channel, and raises on timeout — but the contract fixes only
the call shape, not how the wait is realized. ``timeout`` (sync) and
``expiry_at`` (async) are two ways to bound the wait and are mutually
exclusive; ``check_ask_timing`` enforces that, since a Protocol cannot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from tai42_contract.interactions.models import AnswerMismatchPolicy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from pydantic import BaseModel

    from tai42_contract.interactions.models import FormData, FormPage, MediaItem


def check_ask_timing(*, timeout: float | None, expiry_at: datetime | None) -> None:
    """Reject a ``timeout``+``expiry_at`` combination the ask cannot honor.

    A sync ``timeout`` and an async ``expiry_at`` bound the same wait two ways;
    setting both is a caller error a Protocol cannot catch at runtime.
    """
    if timeout is not None and expiry_at is not None:
        raise ValueError("timeout and expiry_at are mutually exclusive")


@runtime_checkable
class AskUser(Protocol):
    async def __call__(
        self,
        question: str,
        *,
        answer_format: str = "text",
        options: list[str] | None = None,
        schema: type[BaseModel] | dict[str, Any] | None = None,
        data: FormData | dict[str, Any] | None = None,
        pages: list[FormPage] | list[dict[str, Any]] | None = None,
        group_id: str | None = None,
        timeout: float | None = None,
        link: str | Callable[[str], Awaitable[str]] | None = None,
        verifier: dict[str, Any] | None = None,
        channel: str | None = None,
        recipient: str | None = None,
        on_mismatch: AnswerMismatchPolicy = AnswerMismatchPolicy.RETRY,
        mismatch_notice: str | None = None,
        sensitive: bool = False,
        audience: str | None = None,
        media: list[MediaItem | dict[str, Any]] | None = None,
        mode: Literal["sync", "async"] = "sync",
        expiry_at: datetime | None = None,
    ) -> Any:
        """Ask a human ``question``: in ``mode="sync"`` block until the answer
        returns; in ``mode="async"`` park the caller and return a
        ``SuspendedInteraction`` immediately.

        Returns the typed answer per ``answer_format`` (text->str, confirm->bool,
        select->chosen value, form->validated dict). Implementations raise a
        timeout error when the budget elapses with no answer, and ``ValueError``
        for a bad format/argument combination.

        ``link`` is required when ``answer_format="external"`` and forbidden
        otherwise. A ``str`` link is a template containing the literal placeholder
        ``{callback_url}``; a callable receives the callback URL and returns the
        final external URL (creating the external resource is the callable's job).
        The returned answer for external is the payload delivered by the callback
        (a dict from the POST body, or a dict of query params via the GET confirm
        flow), validated against ``schema`` when one was declared.

        ``data`` and ``pages`` enrich a ``form`` ask for ONE send (forbidden on
        every other format). ``data`` (a :class:`FormData` or its dict) prefills
        ``values`` into the form's controls and supplies per-send ``options`` — a
        choice list that REPLACES a property's schema ``enum`` for this send only,
        so a variant needs no re-published form. ``pages`` (a list of
        :class:`FormPage` or their dicts) splits the form into ordered steps, each
        naming the top-level properties it collects; every property appears exactly
        once and absent ``pages`` means one page. Both are validated against the
        form's schema before any state is written; the answer is the union of all
        fields regardless of paging.

        ``verifier`` (``{"name", "config"}``) binds a registered webhook verifier
        to the external callback so the signed server-to-server answer is
        authenticated before it is recorded; it is only valid with
        ``answer_format="external"`` and forbidden otherwise.

        ``channel`` names a registered channel (resolved via
        ``tai42_app.channels``) that delivers the question to a human on an
        out-of-band medium instead of only the Studio inbox; ``None`` keeps the
        inbox-only default. Valid with any ``answer_format`` — when set, the
        implementation mints a callback ticket so the channel can bridge the
        human's reply back — but forbidden together with ``link`` (a channel
        owns its own delivery). ``recipient`` is an OPTIONAL per-call address
        the named channel validates against its operator allowlist — an
        unlisted address makes the ask fail; omitted, the channel sends to
        its operator-configured default recipient. ``recipient`` is forbidden
        when ``channel`` is ``None`` (an address is meaningless without a
        channel to send on).

        ``on_mismatch`` is the digression policy for a guest reply the answer
        door REJECTS on a LIVE channel-delivered ask (a 400 — the reply did not
        fit the question's format). ``AnswerMismatchPolicy.RETRY`` (the default,
        today's behavior) keeps the ask parked and tells the guest what is
        expected so they answer again in place; ``AnswerMismatchPolicy.BRIDGE``
        treats an unmatched reply as a DIGRESSION — it keeps the ask parked with
        NO guest notice and hands the reply to the conversation as a fresh routed
        turn, so the ask ends only by a real answer or its timeout, never by
        unmatched input. It takes effect only on a channel-delivered ask (the
        shared inbound-answer ladder reads it); an inbox-only ask records it but
        never reaches the ladder.

        ``mismatch_notice`` is an OPTIONAL custom guest-facing rejection notice
        used ONLY under the ``RETRY`` policy: when set it REPLACES the platform's
        built-in retry notice. A literal ``{reason}`` token, if present, is filled
        with the door's rejection reason by a plain substitution (a notice without
        the token is sent verbatim; stray braces never raise). It is IGNORED under
        the ``BRIDGE`` policy (a digression never notifies) and by a channel that
        owns its own correction surface (which renders its own text off the door's
        reason). ``None`` (the default) uses the built-in notice; a set value is a
        non-blank string within the guest-reply cap.

        ``sensitive`` marks the answer body as not-to-be-persisted AND wraps the
        returned answer in a ``SecretValue``: the caller reaches the real answer
        only through ``reveal()`` (its repr and JSON dump refuse to expose it),
        while the durable answered record keeps only the status. Use it for
        credentials or personal data.

        ``audience`` is the identity (a user_id) the question is addressed
        to: a restricted caller sees and answers ONLY questions addressed to its
        own identity, while an unrestricted operator sees and may answer
        everything. ``None`` leaves the question unaddressed (an operator/broadcast
        question). It is an identity — a WHO — distinct from ``recipient`` (a
        channel delivery address, a WHERE); both may be set together.

        ``media`` is OPTIONAL display-only context rendered WITH the question in
        the inbox: images and links the human sees when reading the question.
        Each item is ``{kind: "image"|"link", url, caption?}`` — pass a
        ``MediaItem`` or a plain dict (coerced through ``MediaItem`` at request
        construction). A ``link`` url must be an absolute ``http(s)`` URL; an
        ``image`` url must be an absolute ``https`` URL or a ``data:image/*`` URI
        (remote images are https-only — the inbox CSP blocks ``http:`` images). A
        ``data:image/*`` URI is stored by the platform and the durable record keeps
        a served reference to it, not the inline bytes. Every url is a single line —
        whitespace and control/format characters are rejected.
        Caps on the REQUEST (each exceeded value raises, never truncates):
        ``MEDIA_MAX_ITEMS`` is a loose platform abuse guard on the item count (each
        channel refuses anything beyond its own native envelope), each url
        ``<= MEDIA_URL_MAX_CHARS`` (a data: URI ``<= MEDIA_DATA_URI_MAX_CHARS``),
        each caption ``<= MEDIA_CAPTION_MAX_CHARS``, and the summed url text across
        the list ``<= MEDIA_TOTAL_URI_CHARS``. Media never affects the human's
        ANSWER. It renders in the Studio inbox AND, on a channel-delivered ask, rides
        the ``ChannelDelivery.media`` forwarded to the channel plugin (a channel that
        renders media shows it with the question; one that renders only text ignores
        it). A ``data:image/*`` served reference is ABSOLUTE on the channel path (a
        vendor fetches it off-origin), so a channel ask carrying ``data:`` media
        requires ``INTERACTIONS_PUBLIC_BASE_URL``. ``None`` (the default) attaches no
        media; a present list must be non-empty.

        ``mode`` selects the wait discipline: ``"sync"`` (the default) blocks and
        returns the typed answer; ``"async"`` PARKS the caller, returning a
        ``SuspendedInteraction`` sentinel, and the answer/expiry resumes work out
        of band. ``expiry_at`` is the async deadline — the moment a parked question
        expires; async REQUIRES it (a park always carries a deadline) and it is
        mutually exclusive with ``timeout`` (``check_ask_timing`` enforces the
        exclusivity). ``None`` is valid only for a sync ask.
        """
        ...


__all__ = ["AskUser", "check_ask_timing"]
