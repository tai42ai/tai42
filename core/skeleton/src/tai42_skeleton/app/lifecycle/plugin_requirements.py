"""Boot-time check of the platform features each loaded plugin requires.

A plugin names, in its spec's ``requires`` list, the platform features its code cannot work
without, by their gated-feature kind (the ``kind`` of the feature's row in the
``GET /api/system/kinds`` table). Every epoch build checks the spec of every plugin the
deployment loads (:func:`~tai42_skeleton.db.discovery.manifest_plugin_specs`: the manifest's
module imports, its ``studio_plugins`` packages and the config provider ``TAI_CONFIG_MODE``
selects) before any manifest-named module imports: a required feature that is off, or a name
that is no gated-feature kind, refuses the build with one :class:`CorePluginBootError`
naming every unmet requirement. The config provider is the one plugin already imported by
then, because the configuration the build reads is read through it. Each name resolves
through the feature's own ``configured`` predicate, the one its status row reads, so "on"
means the same thing at boot and in the kinds table. An unconfigured feature stays a legal
OFF state for every plugin that does not require it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_skeleton.app.kind_status import gated_features

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tai42_contract.plugins import PluginSpec


def refuse_unmet_plugin_requirements(specs: Iterable[PluginSpec]) -> None:
    """Raise :class:`CorePluginBootError` when any plugin in ``specs`` requires a feature that is off or unknown.

    An off feature is named with the env var that turns it on; an unknown name is named with
    every known feature kind. One refusal lists every unmet requirement of every plugin. The
    marketplace import is function-local to keep the app import chain free of the marketplace
    package.
    """
    from tai42_skeleton.marketplace.compat import CorePluginBootError

    features = gated_features()
    unmet: list[str] = []
    for spec in specs:
        for kind in spec.requires:
            feature = features.get(kind)
            if feature is None:
                unmet.append(
                    f"plugin {spec.ref!r} requires {kind!r}, which is not a platform feature "
                    f"(known features: {', '.join(sorted(features))})"
                )
            elif not feature.configured():
                unmet.append(
                    f"plugin {spec.ref!r} requires the {feature.label} feature ({kind!r}), "
                    f"which is off: set {feature.enabling_var()}"
                )
    if unmet:
        raise CorePluginBootError(
            "a loaded plugin requires a platform feature this deployment does not provide — refusing to start: "
            + "; ".join(unmet)
            + ". Turn the feature on, or stop loading the plugin: remove it from the manifest, "
            + "or set TAI_CONFIG_MODE to another provider when it is the config provider."
        )
