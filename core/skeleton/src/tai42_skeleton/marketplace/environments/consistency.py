"""Whether a plugin layer can sit beside the image: no shared distribution, every requirement met.

Both checks read distributions by canonical name. A distribution the layer and the image both
hold is a conflict however ``sys.path`` orders them (:func:`overlaps`). A requirement a layer
distribution declares must be met by the layer or the image together, its environment marker
evaluated for the running interpreter with no extra selected (:func:`unmet_requirements`).
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from dataclasses import dataclass

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from tai42_skeleton.marketplace.environments.image import reported_version
from tai42_skeleton.marketplace.errors import PluginsDirError

type Distributions = Mapping[str, importlib.metadata.Distribution]


@dataclass(frozen=True)
class Overlap:
    """A distribution both the layer and the image hold."""

    name: str
    layer_version: str
    image_version: str


@dataclass(frozen=True)
class UnmetRequirement:
    """A layer distribution's requirement neither the layer nor the image meets."""

    plugin: str
    requirement: str
    provided: str

    def describe(self) -> str:
        """``<plugin> requires <requirement>; <what provides it>``."""
        return f"{self.plugin} requires {self.requirement}; {self.provided}"


def _version(dist: importlib.metadata.Distribution, where: str) -> str:
    return reported_version(dist, f"{where} distribution", "cannot check the plugin layer against the image")


def overlaps(layer: Distributions, image: Distributions) -> list[Overlap]:
    """Every distribution name the layer and the image both hold, sorted by name."""
    return [
        Overlap(
            name=layer[name].metadata["Name"],
            layer_version=_version(layer[name], "plugin layer"),
            image_version=_version(image[name], "image"),
        )
        for name in sorted(layer.keys() & image.keys())
    ]


def unmet_requirements(layer: Distributions, image: Distributions) -> list[UnmetRequirement]:
    """Every requirement of a layer distribution that the layer and the image together do not meet.

    A requirement that does not parse raises :class:`PluginsDirError` naming the distribution.
    """
    unmet: list[UnmetRequirement] = []
    for name in sorted(layer):
        dist = layer[name]
        plugin = f"{dist.metadata['Name']} {_version(dist, 'plugin layer')}"
        for line in dist.requires or ():
            try:
                requirement = Requirement(line)
            except InvalidRequirement as exc:
                raise PluginsDirError(f"plugin layer distribution {plugin} declares {line!r}: {exc}") from exc
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                continue
            provided = _provided(canonicalize_name(requirement.name), requirement, layer, image)
            if provided is not None:
                unmet.append(UnmetRequirement(plugin=plugin, requirement=str(requirement), provided=provided))
    return unmet


def _provided(name: str, requirement: Requirement, layer: Distributions, image: Distributions) -> str | None:
    """What provides ``requirement`` when it is unmet; ``None`` when it is met."""
    for where, found in (("the plugin layer", layer), ("the image", image)):
        dist = found.get(name)
        if dist is not None:
            version = _version(dist, where.removeprefix("the "))
            if requirement.specifier.contains(version, prereleases=True):
                return None
            return f"{where} provides {dist.metadata['Name']} {version}"
    return f"neither the plugin layer nor the image provides {requirement.name}"
