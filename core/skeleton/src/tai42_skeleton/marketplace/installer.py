"""The marketplace installer — resolve, install, patch, reload, attribute.

:class:`Installer` orders the ``install`` / ``uninstall`` / ``update`` flows (plus
``preview`` and ``upgrade_all``) as abort-on-any-step sequences with an explicit
reverse unwind, delegating each concern to a sibling module (``locks``, ``resolve``,
``environments``, ``manifest_apply``, ``env_apply``, ``route_preflight``, ``notes``,
``unwind``). Every collaborator is injected so a test can fake each seam.

The flows themselves live in focused sibling modules — the shared constructor and
config and plugins-directory seams in :mod:`~tai42_skeleton.marketplace.installer_base`, the install and
uninstall flows in :mod:`~tai42_skeleton.marketplace.installer_install`, the update and
upgrade-all flows in :mod:`~tai42_skeleton.marketplace.installer_update` — and this
class composes them into the one public installer.

Every manifest write crosses :class:`~tai42_skeleton.config.service.ConfigService`,
reached FRESH per use because a reload swaps app internals. A packaged plugin's
environments are built beside the ones in use and swapped in, so an unwind restores the
previous builds with no pip call.
"""

from __future__ import annotations

from tai42_skeleton.marketplace.installer_install import _InstallFlow
from tai42_skeleton.marketplace.installer_update import _UpdateFlow


class Installer(_InstallFlow, _UpdateFlow):
    """Install, uninstall, and update marketplace plugins with abort-and-unwind.

    Each public method acquires the per-worker fast-path lock and then the
    fleet-wide advisory lock before touching any state, and holds both across the
    whole operation, so a lock-held-elsewhere refusal makes no store, registry,
    pip, or manifest call. The fleet lock is held exclusively while the operation
    changes the plugins directory, the manifest and the install store, and shared
    while it then rolls the fleet recycle those changes owe
    (:func:`~tai42_skeleton.marketplace.operation_roll.guarded_operation`).
    """
