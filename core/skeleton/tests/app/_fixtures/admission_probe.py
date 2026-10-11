"""Fixture tool module: a tool that records which request admission it runs on behalf of.

Each call appends, at the moment the tool body runs, the record
``current_request_admission`` resolves, whether that record's request had already ended,
and the record the admission ContextVar holds in the tool body's own context.
"""

from dataclasses import dataclass

from tai42_contract.app import tai42_app

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.epoch import RequestAdmission, current_request_admission


@dataclass(frozen=True)
class Observation:
    """What one call of :func:`admission_probe` saw while it ran."""

    resolved: RequestAdmission | None
    resolved_finished: bool
    context_record: RequestAdmission | None
    context_record_finished: bool


OBSERVED: list[Observation] = []


@tai42_app.tools.tool
async def admission_probe() -> int:
    """Record the request admission this call resolves and return the call's ordinal."""
    resolved = current_request_admission()
    context_record = epoch_mod._admission.get()
    OBSERVED.append(
        Observation(
            resolved=resolved,
            resolved_finished=resolved is not None and resolved.finished.is_set(),
            context_record=context_record,
            context_record_finished=context_record is not None and context_record.finished.is_set(),
        )
    )
    return len(OBSERVED)
