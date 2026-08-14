"""The hard bar, executable: no engine leaks into the shared surface.

Any engine-specific noun in ``tai42_kit.backend`` or in the contract's runtime
module means the "generic" base has quietly grown a special case for one vendor,
which is how a shared base stops being shareable. Reviewing for it does not
scale; scanning the source does.
"""

from __future__ import annotations

import re
from pathlib import Path

import tai42_contract.backend.runtime as contract_runtime

import tai42_kit.backend as kit_backend
import tai42_kit.signals as kit_signals

# Engine names, engine-internal machinery, and the vendor libraries behind them —
# including the engines this codebase does NOT ship, so the guard keeps holding
# when one of them arrives. The boundaries are hand-written rather than ``\b``
# because an underscore is a word character: ``\bcelery\b`` would miss
# ``celery_app``, which is exactly the shape a leak takes.
_VENDOR_TOKENS = re.compile(
    r"(?<![A-Za-z0-9])("
    r"rq|celery|arq|redis|billiard|kombu|flower|prefork|gevent|horse|beat"
    r"|taskiq|dramatiq|huey|amqp"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _shared_sources() -> list[Path]:
    """Every file a backend binding depends on and cannot change: the shared
    package, the contract's runtime declarations, and the signal chain the
    lifecycle composes through."""
    package = Path(kit_backend.__path__[0])
    sources = sorted(package.rglob("*.py"))
    assert sources, "the shared backend package has no sources to scan"
    for module in (contract_runtime, kit_signals):
        assert module.__file__
        sources.append(Path(module.__file__))
    return sources


def test_no_engine_name_reaches_the_shared_surface() -> None:
    offenders: list[str] = []
    for path in _shared_sources():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            match = _VENDOR_TOKENS.search(line)
            if match:
                offenders.append(f"{path.name}:{lineno}: {match.group(0)!r} in {line.strip()!r}")

    assert not offenders, offenders


def test_the_scan_would_catch_a_leak() -> None:
    # The guard is only worth its line count if it actually matches; assert the
    # pattern on a line shaped like the leak it exists to stop.
    assert _VENDOR_TOKENS.search("    self._worker = celery_app.Worker()")
    assert _VENDOR_TOKENS.search("# the prefork pool must be restarted")
    assert _VENDOR_TOKENS.search("    self._conn = Redis.from_url(url)")
    assert _VENDOR_TOKENS.search("    if self._worker.horse_pid:")
    assert _VENDOR_TOKENS.search('    runtimes = {"beat": BeatRuntime}')
    assert _VENDOR_TOKENS.search("    from taskiq import AsyncBroker")
    # Ordinary English that merely contains a token is not a leak.
    assert not _VENDOR_TOKENS.search("    the request requires a marquee budget")
    assert not _VENDOR_TOKENS.search("    the health_check heartbeat is unrelated")
