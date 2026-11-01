"""Verifying the plugins directory against the install store once per process, under the shared fleet lock.

:func:`verify_environments_at_boot` runs as the last step of the first
:func:`~tai42_skeleton.app.lifecycle.manifest_imports.prepare_manifest_imports` call in a
process, after the schema gate, so a pending skeleton chain refuses with ``tai db migrate``
before the install store is read. A later call in the same process (a reload, an epoch
rebuild) returns at once: the directory changes only through a writer holding the exclusive
fleet lock, and that writer drives those reloads itself.

The boundary: a process that is already up and reloads after a writer stopped between the
``current`` swap and the install-store write activates the swapped-in build without refusing;
the next process start refuses and names ``tai plugins rebuild``.
"""

from __future__ import annotations

import logging

from tai42_kit.db import component_store_configured

from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.marketplace.compat import CorePluginBootError
from tai42_skeleton.marketplace.environments.settings import configured_plugins_dir
from tai42_skeleton.marketplace.environments.verify import verify_environments
from tai42_skeleton.marketplace.locks import fleet_lock_shared
from tai42_skeleton.marketplace.store import MarketplaceInstallStore

logger = logging.getLogger(__name__)


class _BootVerification:
    """Whether this process has verified its plugin environments."""

    done: bool = False


_VERIFIED = _BootVerification()


async def verify_environments_at_boot() -> None:
    """Refuse to start when the plugins directory does not match the install store; once per process.

    With no skeleton database configured there are no install rows to verify against: an
    info line says so and the process counts as verified. Otherwise the rows are read and the
    directory verified under :func:`~tai42_skeleton.marketplace.locks.fleet_lock_shared`, so a
    writer still changing the directory and the store finishes those changes first. A writer
    that rolls a fleet recycle holds the key shared while it rolls, so a worker the roll
    restarts is granted its hold at once and verifies the finished change. Any finding raises
    :class:`~tai42_skeleton.marketplace.compat.CorePluginBootError` whose message is every
    finding's boot text, one per line. The process is marked verified only after a pass, so a
    refused start retried in the same process verifies again. A configured database that
    cannot be reached raises as itself. Never writes the directory.
    """
    if _VERIFIED.done:
        return
    if not component_store_configured(SKELETON_COMPONENT):
        logger.info(
            "plugin environments: the %r database is not configured — skipping their verification",
            SKELETON_COMPONENT,
        )
        _VERIFIED.done = True
        return
    async with fleet_lock_shared():
        rows = await MarketplaceInstallStore().list_installed()
        verification = verify_environments(configured_plugins_dir(), rows)
    findings = verification.findings
    if findings:
        raise CorePluginBootError("\n".join(finding.boot_text() for finding in findings))
    _VERIFIED.done = True
