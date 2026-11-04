"""The live manifest reads resolve the LIVE generation while a rebuild builds the next one.

A rebuild installs the core of the generation under construction on its epoch before its
``start()`` records that generation's manifest, so a read that resolved the core being built
would find no manifest at all. The admin facet's live-manifest reads answer the manifest of the
generation still serving, whoever asks. The build itself keeps reading the manifest of the
generation it builds through ``_require_manifest``.
"""

from __future__ import annotations

import asyncio

from tai42_skeleton.app import instance
from tai42_skeleton.app.epoch import Epoch, building
from tai42_skeleton.manifest import Manifest


def _manifest(*studio_plugins: str) -> Manifest:
    return Manifest.model_validate(
        {
            "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather"]}],
            "studio_plugins": list(studio_plugins),
        }
    )


def test_the_live_manifest_reads_during_a_rebuild_answer_the_live_generation() -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            live = instance.app._require_live_manifest()
            # The work of a rebuild whose generation's own start() has not run yet.
            with building(Epoch(number=0, core=instance.app._build_serving_core())):
                dumped = instance.app.admin.live_manifest
                typed = instance.app.admin.live_manifest_typed

            assert typed is live
            assert dumped == live.live_manifest.model_dump(mode="json", exclude_none=True)

    asyncio.run(run())


def test_the_build_reads_the_manifest_of_the_generation_it_builds() -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            core = instance.app._build_serving_core()
            core._manifest = _manifest("acme_plugin")
            with building(Epoch(number=0, core=core)):
                built = instance.app._require_manifest()
                live = instance.app.admin.live_manifest_typed

            assert built is core._manifest
            assert built.studio_plugins == ["acme_plugin"]
            assert live.studio_plugins == []

    asyncio.run(run())
