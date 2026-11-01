"""Whether an environment's ``current`` build has a record that reads: the one reader of that fact.

:func:`current_record` is how every reader of a ``current`` build's record gets it — the
verification (:mod:`.verify`) and the import seam's snapshot of the plugins the layer left out
(:func:`~tai42_skeleton.marketplace.compat.layer_exclusions`) — so a record that is missing or
does not read is one :class:`~.findings.Finding`, worded the same wherever it is met.
"""

from __future__ import annotations

from dataclasses import dataclass

from tai42_skeleton.marketplace.environments.findings import REBUILD_DEPLOYMENT, Finding
from tai42_skeleton.marketplace.environments.layout import BUILD_RECORD, BuildRecord, EnvironmentDir, read_build_record
from tai42_skeleton.marketplace.environments.swap import current_build
from tai42_skeleton.marketplace.errors import UnreadableBuildRecordError


@dataclass(frozen=True)
class CurrentRecord:
    """An environment's ``current`` build id and its record, or the finding when that record does not read.

    ``build_id`` is ``None`` when the environment has no ``current`` build; then ``record`` and
    ``finding`` are ``None`` too. With a ``current`` build exactly one of ``record`` and
    ``finding`` is set.
    """

    build_id: str | None
    record: BuildRecord | None
    finding: Finding | None


def current_record(environment: EnvironmentDir, subject: str) -> CurrentRecord:
    """Read ``environment``'s ``current`` build record; ``subject`` names the environment in the finding.

    A ``current`` build without a record is incomplete, and one whose record is not the
    document a build writes does not read: each is a finding naming the record file and
    ``tai plugins rebuild``.
    """
    build_id = current_build(environment)
    if build_id is None:
        return CurrentRecord(build_id=None, record=None, finding=None)
    build = environment.build(build_id)
    try:
        record = read_build_record(build)
    except UnreadableBuildRecordError as exc:
        finding = Finding(
            head=f"{subject} has an unreadable build record {exc.path}", detail=exc.reason, tail=REBUILD_DEPLOYMENT
        )
        return CurrentRecord(build_id=build_id, record=None, finding=finding)
    if record is None:
        finding = Finding(
            head=f"{subject} has no build record {build / BUILD_RECORD}",
            detail=f"its current build {build_id} is incomplete",
            tail=REBUILD_DEPLOYMENT,
        )
        return CurrentRecord(build_id=build_id, record=None, finding=finding)
    return CurrentRecord(build_id=build_id, record=record, finding=None)
