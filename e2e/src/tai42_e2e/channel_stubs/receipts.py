"""Delivery receipts a fake vendor posts on its own, the way the real medium reports each
accepted message delivered: a genuinely signed ``delivered`` status webhook sent to the
deployment's status door right after the send's response. A send whose body carries a
withheld needle gets no receipt, so a test can post that message's status itself."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from tai42_e2e.provider_stub import SignedInbound


@dataclass(frozen=True)
class ReceiptDoor:
    """Where a fake vendor posts its receipts: the deployment's status door ``url`` and the
    ``secret`` the vendor signs with (the URL is part of the signature where the vendor signs it)."""

    url: str
    secret: str


class AutoReceipts:
    """One stub's automatic receipts: off until a :class:`ReceiptDoor` is set.

    Every post that does not answer 2xx is kept until :meth:`drain_failures` takes it, and
    :meth:`reset` keeps them, so a receipt the deployment refused after its test ended still
    reaches the harness, which fails on any."""

    def __init__(self) -> None:
        self.door: ReceiptDoor | None = None
        self._failures: list[str] = []
        self._withheld: set[str] = set()

    def reset(self) -> None:
        self.door = None
        self._withheld.clear()

    def drain_failures(self) -> list[str]:
        """Every refused or unreachable receipt post since the last drain."""
        failures, self._failures = self._failures, []
        return failures

    def withhold(self, needle: str) -> None:
        """Post no receipt for any send whose body carries ``needle``."""
        self._withheld.add(needle)

    def due(self, body: str) -> ReceiptDoor | None:
        """The door to post a receipt for a send carrying ``body`` to, or ``None`` when none is due."""
        if self.door is None or any(needle in body for needle in self._withheld):
            return None
        return self.door

    async def post(self, url: str, status: SignedInbound, provider_id: str) -> None:
        """POST the signed ``status`` to ``url``; a refused or unreachable post is a failure."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, content=status.body, headers=status.headers)
        except httpx.HTTPError as exc:
            self._failures.append(f"receipt for {provider_id} to {url}: {exc!r}")
            return
        if not resp.is_success:
            self._failures.append(f"receipt for {provider_id} to {url}: HTTP {resp.status_code} {resp.text}")
