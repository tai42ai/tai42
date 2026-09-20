"""Scrape and parse Prometheus exposition, and look samples up by family +
labels. Used against both the standalone metrics server (the multiproc reader)
and a worker's in-app ``GET /metrics``."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Scrape:
    """A parsed metrics snapshot; sample values are keyed by family name + the
    exact label set."""

    raw: str

    def sample(self, family: str, labels: dict[str, str]) -> float | None:
        """The value of the sample of ``family`` whose labels are a superset of
        ``labels``, or ``None`` if no such sample exists. ``None`` (not 0.0)
        distinguishes "family/label absent" from "present and zero"."""
        # Deferred: importing prometheus_client freezes its value backend
        # (multiprocess mmap vs in-process mutex) at first import. Keeping it out of
        # this module's import graph lets the pytest11 entry point register without
        # freezing that choice before a conftest sets PROMETHEUS_MULTIPROC_DIR.
        from prometheus_client.parser import text_string_to_metric_families

        for parsed in text_string_to_metric_families(self.raw):
            for s in parsed.samples:
                if s.name != family:
                    continue
                if all(s.labels.get(k) == v for k, v in labels.items()):
                    return s.value
        return None

    def require(self, family: str, labels: dict[str, str]) -> float:
        """Like :meth:`sample` but raises if the sample is absent — for
        assertions that a sample MUST exist."""
        value = self.sample(family, labels)
        if value is None:
            raise AssertionError(f"no sample {family}{labels} in scrape:\n{self.raw[:2000]}")
        return value


def scrape(url: str, *, timeout: float = 5.0) -> Scrape:
    """GET the ``/metrics`` endpoint at ``url`` and parse it."""
    response = httpx.get(url, timeout=timeout)
    response.raise_for_status()
    return Scrape(raw=response.text)
