"""The marketplace installer — resolve, install, patch, reload, attribute.

:class:`Installer` orders the ``install`` / ``uninstall`` / ``update`` flows (plus
``preview`` and ``upgrade_all``) as abort-on-any-step sequences with an explicit
reverse unwind, delegating each concern to a sibling module (``locks``, ``resolve``,
``package_ops``, ``manifest_apply``, ``env_apply``, ``route_preflight``, ``notes``,
``unwind``). Every collaborator is injected so a test can fake each seam.

The flows themselves live in focused sibling modules — the shared constructor and
config/venv seams in :mod:`~tai42_skeleton.marketplace.installer_base`, the install and
uninstall flows in :mod:`~tai42_skeleton.marketplace.installer_install`, the update and
upgrade-all flows in :mod:`~tai42_skeleton.marketplace.installer_update` — and this
class composes them into the one public installer.

Every manifest write crosses :class:`~tai42_skeleton.config.service.ConfigService`,
reached FRESH per use because a reload swaps app internals. Only skeleton state fully
reverts on an unwind; the venv is only as transactional as pip itself (the
pip-transaction caveat rides the response text).
"""

from __future__ import annotations

from tai42_skeleton.marketplace.installer_install import _InstallFlow
from tai42_skeleton.marketplace.installer_update import _UpdateFlow


class Installer(_InstallFlow, _UpdateFlow):
    """Install, uninstall, and update marketplace plugins with abort-and-unwind.

    Each public method acquires the per-worker fast-path lock and then the
    fleet-wide advisory lock before touching any state, and holds both across the
    whole operation, so a lock-held-elsewhere refusal makes no store, registry,
    pip, or manifest call.
    """
