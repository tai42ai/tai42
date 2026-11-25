"""The media bodies and the byte-serving behaviour the fake vendors share for inbound-media fetches."""

from __future__ import annotations

import base64
from dataclasses import dataclass

from fastapi import Response

# Deterministic media bodies the fake vendors serve for the inbound-media ingest suites: a valid
# 1x1 PNG (sniffs ``image/png``), a minimal PDF (sniffs ``application/pdf``), and an HTML buffer
# (sniffs to nothing — the disallowed-content case under a declared image type).
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
TINY_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
HTML_BODY = b"<!DOCTYPE html><html><body><h1>not an image</h1></body></html>"


@dataclass
class MediaBlob:
    """One fake vendor's byte-serving behaviour for a single media reference.

    ``body`` + ``content_type`` are the healthy 200 response. A ``status`` other than 200 answers
    that status with no body (a 404 miss, a 5xx). ``fail_times`` serves ``fail_status`` for the
    first N fetches of the reference and the healthy body thereafter — the transient-then-redeliver
    case (the fake fails once, the vendor redelivers, the retry succeeds)."""

    body: bytes = b""
    content_type: str | None = None
    status: int = 200
    fail_status: int = 502
    fail_times: int = 0


def serve_blob(blob: MediaBlob, served: dict[str, int], ref: str) -> Response:
    """Answer one configured media fetch for ``ref``, counting fetches so ``fail_times`` can serve
    the transient fault first and the healthy body on the redelivery."""
    count = served.get(ref, 0)
    served[ref] = count + 1
    if blob.fail_times and count < blob.fail_times:
        return Response(status_code=blob.fail_status)
    if blob.status != 200:
        return Response(status_code=blob.status)
    return Response(content=blob.body, media_type=blob.content_type)
