import logging
import mimetypes
import uuid
from io import BytesIO
from urllib.parse import urlparse

from httpx import AsyncClient, HTTPStatusError

from tai42_kit.net import fetch_url, url_guard
from tai42_kit.net.url_guard import UrlGuardError

logger = logging.getLogger(__name__)

# How many bytes of an HTTP error body to include in the error log. The body of
# a failed guarded download is streaming and never read in full, so the snippet
# is bounded by construction.
_ERROR_BODY_SNIPPET_BYTES = 1000


async def url_to_filelike(
    url: str, client: AsyncClient | None = None, *, guard: bool = True
) -> tuple[str, BytesIO, str | None]:
    """Download ``url`` into a named in-memory file: ``(filename, BytesIO, content_type)``.

    The default path (no ``client``) fetches through :func:`tai42_kit.net.fetch_url`
    and carries the SSRF guard at full strength: DNS-rebinding pinning, per-hop
    guarded redirect following, and the response-size cap. Reaching an internal
    host, exceeding the size cap, or exhausting the redirect budget raises rather
    than downloading it.

    With an injected ``client`` and ``guard=True`` (the default), a
    transport-independent pre-flight runs before the download: the URL host is
    resolved and validated through the SSRF guard (a URL without a host raises
    :class:`~tai42_kit.net.url_guard.UrlGuardError`), then the body is streamed on
    the caller's client with the guard's size cap enforced per chunk. This is
    honestly weaker than the default path: the pre-flight validates what the host
    resolves to at check time, but the caller's client re-resolves at connect time
    (so DNS rebinding is not closed) and applies its own redirect policy without
    per-hop validation — a followed hop's target is not re-validated and its
    intermediate body is not size-capped; only the terminal body is. Full
    strength — connection pinning and per-hop redirect validation — is only the
    default no-client path.

    ``guard=False`` is the explicit caller-owns-policy opt-out and needs an
    injected ``client`` to own that policy (``ValueError`` without one): the
    download runs on the supplied client with NONE of the guard's protections —
    no host validation, no size cap, and whatever target and redirect policy that
    client applies. Pass it only when the caller enforces its own policy.

    The guard's global kill switch wins on every path: with
    ``TAI_URL_GUARD_ENABLED=false`` nothing is validated or capped regardless of
    ``guard``; deliberate internal fetches are opted in through
    ``TAI_URL_GUARD_ALLOW_CIDRS``.

    The filename is a random UUID; its extension is guessed from the response
    ``Content-Type`` (``None`` when absent), which is also returned verbatim.
    """
    if client is None:
        if not guard:
            raise ValueError(
                "guard=False needs an injected client to own the download policy; "
                "the default path always fetches through the guarded fetch_url."
            )
        content, content_type = await fetch_url(url)
    elif guard and url_guard.guard_enabled():
        content, content_type = await _guarded_client_fetch(url, client)
    else:
        resp = await client.get(url)
        try:
            resp.raise_for_status()
        except HTTPStatusError as e:
            logger.error(
                "HTTPError for %s: %s, %s %s: %s", url, str(e), resp.status_code, resp.reason_phrase, resp.text[:1000]
            )
            raise
        content = resp.content
        content_type = resp.headers.get("Content-Type")

    # guess_extension needs the bare MIME type; a full Content-Type may carry
    # parameters (``image/png; charset=binary``) that would otherwise make it
    # return None and silently drop the extension.
    mime_type = (content_type or "").split(";", 1)[0].strip()
    extension = mimetypes.guess_extension(mime_type)
    filename = f"{uuid.uuid4()}{extension or ''}"
    return filename, BytesIO(content), content_type


async def _guarded_client_fetch(url: str, client: AsyncClient) -> tuple[bytes, str | None]:
    """Download ``url`` on the caller's ``client`` behind the SSRF guard's
    transport-independent pre-flight: resolve and validate the URL host first
    (no host raises :class:`UrlGuardError`), then stream the body enforcing the
    guard's size cap per chunk."""
    host = urlparse(url).hostname
    if not host:
        raise UrlGuardError(f"SSRF guard: URL has no host to check: {url!r}")
    await url_guard.resolve_and_validate(host)

    async with client.stream("GET", url) as resp:
        try:
            resp.raise_for_status()
        except HTTPStatusError as e:
            # The body is streaming and deliberately never read in full on error;
            # log a bounded snippet of it for diagnosis.
            snippet = bytearray()
            async for chunk in resp.aiter_bytes():
                snippet.extend(chunk)
                if len(snippet) >= _ERROR_BODY_SNIPPET_BYTES:
                    break
            logger.error(
                "HTTPError for %s: %s, %s %s: %s",
                url,
                str(e),
                resp.status_code,
                resp.reason_phrase,
                bytes(snippet[:_ERROR_BODY_SNIPPET_BYTES]).decode("utf-8", errors="replace"),
            )
            raise
        buffer = bytearray()
        async for chunk in resp.aiter_bytes():
            buffer.extend(chunk)
            url_guard.enforce_size(len(buffer))
        return bytes(buffer), resp.headers.get("Content-Type")
