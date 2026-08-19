"""Runtimes that install their own signal handler — the red cases for the scan.

They live in a module of their own on purpose: the certifier reads the whole
MODULE a runtime is defined in (a ``request_drain`` that delegates to a
module-level helper installs a handler just as effectively), so co-locating
these with the clean fakes would make every runtime in that file report the
leak.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Mapping
from typing import ClassVar

from tai42_contract.backend.runtime import BackendRuntime

from .fakes import FakeOnLoopBackend, FakeOnLoopWorker

_Runtimes = Mapping[str, type[BackendRuntime]]


class SelfWiringWorker(FakeOnLoopWorker):
    """Rebinds the signal at C level, which removes asyncio's signal machinery
    and with it every other subscriber on the chain."""

    def request_drain(self) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)


class SelfWiringBackend(FakeOnLoopBackend):
    runtimes: ClassVar[_Runtimes] = {"worker": SelfWiringWorker}


class DelegatingWorker(FakeOnLoopWorker):
    """Declares nothing suspicious in its own body: the install is one call away,
    in this module. Only the module-wide read catches it."""

    async def build(self) -> None:
        _install_out_of_line()


class DelegatingBackend(FakeOnLoopBackend):
    runtimes: ClassVar[_Runtimes] = {"worker": DelegatingWorker}


def _install_out_of_line() -> None:
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: None)
