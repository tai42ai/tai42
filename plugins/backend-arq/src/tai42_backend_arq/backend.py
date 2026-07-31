"""The arq :class:`~tai42_contract.backend.Backend` implementation.

``launch`` starts the arq worker runtime for a ``worker`` subcommand, parsing
its options through the worker CLI.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from tai42_contract.app import tai42_app
from tai42_contract.backend import Backend


class ArqBackend(Backend):
    """arq execution backend: the worker runtime that executes enqueued work."""

    async def launch(self, args: Sequence[str]) -> None:
        if not args:
            print("Usage: arq worker [options]")
            sys.exit(1)

        subcmd, *rest = args

        if subcmd != "worker":
            print(f"Unknown ARQ command: {subcmd}")
            sys.exit(1)

        from tai42_backend_arq import worker

        # Parse strictly: an unknown/malformed option aborts the launch loudly.
        ctx = worker.main.make_context("arq-worker", list(rest))
        await worker.start_arq_worker(**ctx.params)


# Plain call (not decorator) so ``ArqBackend`` keeps its concrete class type.
tai42_app.backends.register_backend(ArqBackend)
