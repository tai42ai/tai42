"""The reload admission every turn entry point runs first."""

from __future__ import annotations

from tai42_contract.errors import ServerReloadingError

from tai42_skeleton.app.reload_gate import REJECT_MESSAGE, reload_gate


def refuse_turn_while_reloading() -> None:
    """Refuse a new turn while a config reload holds the reload gate.

    Called before any record, claim or caps slot is written, so a refused turn leaves nothing
    behind and a redelivery is processed in full. :class:`ServerReloadingError` is retriable and
    is neither a ``ValueError`` nor a ``LookupError``, so no channel door acknowledges and drops it.
    """
    if reload_gate.locked:
        raise ServerReloadingError(REJECT_MESSAGE)
